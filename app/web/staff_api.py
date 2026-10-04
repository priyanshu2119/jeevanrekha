"""JSON API for the staff mobile apps (ASHA + operator).

The web staff pages are server-rendered HTML; the Android app needs the
same information as compact JSON over token auth, pollable on flaky 2G/3G
and wakeable by FCM push. One overview payload powers the home screen (one
round trip per poll -- deliberate for low-bandwidth field conditions):

    GET /api/staff/overview
      -> active dispatch cases, actionable pending alerts, operator
         follow-ups, recent calls -- all scoped to the signed-in role
         (ASHA sees only her region, exactly like the /asha page).

Auth: POST /api/auth/login returns the same itsdangerous-signed token the
web session cookie carries; the app sends it as Authorization: Bearer.
deps.request_user accepts either form, so roles/scoping behave identically.

Desk actions mirror the simulator desk (same service calls a provider
webhook would make), with one addition that matters in the field: an ASHA
can confirm/decline an alert for HER region straight from the app -- she
is the responder the chain is waiting for, and making her answer a phone
call to a desk operator would waste the confirmation window.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..engine.questions import QUESTION_INDEX
from ..models import (
    Answer,
    CallSession,
    CallStatus,
    DeviceToken,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    DispatchStatus,
    DispatchTrack,
    Region,
    Role,
    TriageResult,
    User,
    utcnow,
)
from ..security import make_session_token, verify_password
from ..services import dispatch as dispatch_service
from ..services.dispatch import ist_str
from . import serializers
from .deps import request_user

router = APIRouter(prefix="/api")
log = logging.getLogger("jr.staff_api")


def _require_user(request: Request, db: Session) -> User:
    user = request_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def _user_json(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "region_id": user.region_id,
        "region": user.region.label if user.region else None,
        "language": user.language,
    }


# --------------------------------------------------------------------------
# Auth + device registration
# --------------------------------------------------------------------------

class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def api_login(body: LoginBody, db: Session = Depends(get_db)):
    user = db.scalars(
        select(User).where(User.username == body.username.strip())
    ).first()
    if user is None or not user.is_active or not verify_password(body.password, user):
        # Same posture as the web form: no hint about which half was wrong.
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"token": make_session_token(user.id), "user": _user_json(user)}


class DeviceBody(BaseModel):
    token: str
    platform: str = "android"
    label: str | None = None


@router.post("/devices")
def register_device(body: DeviceBody, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=422, detail="token required")
    row = db.scalars(select(DeviceToken).where(DeviceToken.token == token)).first()
    if row is None:
        row = DeviceToken(token=token, user_id=user.id,
                          platform=body.platform or "android",
                          label=(body.label or "")[:120] or None)
        db.add(row)
    else:
        # Tokens rotate and devices change hands: always rebind to whoever
        # is logged in now, so alerts never follow the previous user.
        row.user_id = user.id
        row.platform = body.platform or "android"
        row.label = (body.label or "")[:120] or None
        row.is_active = True
        row.last_seen_at = utcnow()
    db.commit()
    return {"ok": True}


@router.delete("/devices")
def unregister_device(body: DeviceBody, request: Request, db: Session = Depends(get_db)):
    _require_user(request, db)
    row = db.scalars(
        select(DeviceToken).where(DeviceToken.token == body.token.strip())
    ).first()
    if row is not None:
        row.is_active = False
        db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Live overview (the app home screen; polled in foreground, woken by FCM
# in background)
# --------------------------------------------------------------------------

def _scoped_region_id(user: User) -> int | None:
    """ASHA: her region only. Operator/admin: None = everything."""
    if user.role == Role.asha.value:
        return user.region_id or -1  # -1: ASHA without a region sees nothing
    return None


@router.get("/staff/overview")
def staff_overview(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    scope_region = _scoped_region_id(user)

    # --- active dispatch cases (+ recently confirmed, so the app can show
    # the outcome for a few minutes instead of the case vanishing) ---------
    cases = db.scalars(
        select(DispatchCase)
        .where(
            DispatchCase.status.in_([
                DispatchStatus.active.value,
                DispatchStatus.confirmed.value,
            ])
        )
        .order_by(DispatchCase.id.desc())
        .limit(50)
    ).all()

    active = []
    for case in cases:
        call = db.get(CallSession, case.call_id)
        if call is None:
            continue
        if scope_region is not None and call.region_id != scope_region:
            continue
        if (case.status == DispatchStatus.confirmed.value
                and case.confirmed_at
                and (utcnow() - case.confirmed_at).total_seconds() > 1800):
            continue  # confirmed more than 30 min ago: history, not live
        summary = serializers.dispatch_summary(db, call)
        active.append({
            "case_id": case.id,
            "call_ref": call.ref_code,
            "status": case.status,
            "region": call.region.label if call.region else None,
            "language": call.language,
            "started_at": ist_str(call.started_at),
            "ambulance": (summary or {}).get("ambulance"),
            "backup": (summary or {}).get("backup"),
            "operator_alerts": (summary or {}).get("operator_alerts", 0),
        })

    # --- pending alerts this user can act on -------------------------------
    pending = db.scalars(
        select(DispatchEvent)
        .where(
            DispatchEvent.action == DispatchAction.call_placed.value,
            DispatchEvent.resolved.is_(False),
        )
        .order_by(DispatchEvent.id.desc())
        .limit(50)
    ).all()
    alerts = []
    for ev in pending:
        case = db.get(DispatchCase, ev.case_id)
        call = db.get(CallSession, case.call_id) if case else None
        if call is None:
            continue
        if scope_region is not None and call.region_id != scope_region:
            continue
        alerts.append({
            "event_id": ev.id,
            "case_id": ev.case_id,
            "call_ref": call.ref_code,
            "track": ev.track,
            "contact_name": ev.contact_name,
            "contact_kind": ev.contact_kind,
            "attempt": ev.attempt,
            "due_at": ist_str(ev.due_at),
            "region": call.region.label if call.region else None,
        })

    # --- operator follow-up queue (operators/admins only) -------------------
    ops = []
    if user.role in (Role.operator.value, Role.admin.value):
        rows = db.scalars(
            select(DispatchEvent)
            .where(
                DispatchEvent.track == DispatchTrack.operator.value,
                DispatchEvent.action.in_([
                    DispatchAction.operator_alert.value,
                    DispatchAction.exhausted.value,
                ]),
                DispatchEvent.resolved.is_(False),
            )
            .order_by(DispatchEvent.id.desc())
            .limit(20)
        ).all()
        for ev in rows:
            case = db.get(DispatchCase, ev.case_id)
            call = db.get(CallSession, case.call_id) if case else None
            ops.append({
                "event_id": ev.id,
                "call_ref": call.ref_code if call else "?",
                "action": ev.action,
                "detail": ev.detail,
                "at": ist_str(ev.created_at),
            })

    # --- recent calls (scoped) ----------------------------------------------
    q = select(CallSession).order_by(CallSession.id.desc()).limit(20)
    if scope_region is not None:
        q = q.where(CallSession.region_id == scope_region)
    recent = []
    for c in db.scalars(q).all():
        recent.append({
            "ref": c.ref_code,
            "status": c.status,
            "channel": c.channel,
            "region": c.region.label if c.region else None,
            "started_at": ist_str(c.started_at),
            "tier": c.result.tier if c.result else None,
        })

    return {
        "user": _user_json(user),
        "server_time": ist_str(utcnow()),
        "active_cases": active,
        "pending_alerts": alerts,
        "operator_alerts": ops,
        "recent_calls": recent,
    }


# --------------------------------------------------------------------------
# Call detail (answers, triage, full dispatch timeline)
# --------------------------------------------------------------------------

@router.get("/staff/calls/{ref}")
def staff_call_detail(ref: str, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    call = db.scalars(select(CallSession).where(CallSession.ref_code == ref)).first()
    if call is None:
        raise HTTPException(status_code=404, detail="not_found")
    scope_region = _scoped_region_id(user)
    if scope_region is not None and call.region_id != scope_region:
        # Same posture as the web view: out-of-scope records do not exist.
        raise HTTPException(status_code=404, detail="not_found")

    answers = []
    for a in db.scalars(select(Answer).where(Answer.call_id == call.id)
                        .order_by(Answer.id)).all():
        q = QUESTION_INDEX.get(a.question_id)
        answers.append({
            "question_id": a.question_id,
            "value": a.value,
            "ambiguous": a.ambiguous,
            "severity": q.severity if q else None,
            "at": ist_str(a.answered_at),
        })
    result = db.scalars(
        select(TriageResult).where(TriageResult.call_id == call.id)
    ).first()
    timeline = []
    case = db.scalars(
        select(DispatchCase).where(DispatchCase.call_id == call.id)
    ).first()
    if case:
        for ev in db.scalars(select(DispatchEvent)
                             .where(DispatchEvent.case_id == case.id)
                             .order_by(DispatchEvent.id)).all():
            timeline.append({
                "at": ist_str(ev.created_at),
                "track": ev.track,
                "action": ev.action,
                "attempt": ev.attempt,
                "contact": ev.contact_name,
                "detail": ev.detail,
            })
    return {
        "call": {
            "ref": call.ref_code,
            "status": call.status,
            "channel": call.channel,
            "region": call.region.label if call.region else None,
            "language": call.language,
            "track": call.track,
            "started_at": ist_str(call.started_at),
            "ended_at": ist_str(call.ended_at),
        },
        "answers": answers,
        "result": {
            "tier": result.tier,
            "flagged": result.flagged,
            "unclear_count": result.unclear_count,
            "guidance": result.guidance,
            "engine_version": result.engine_version,
            "fail_safe": result.fail_safe,
        } if result else None,
        "dispatch": serializers.dispatch_summary(db, call),
        "timeline": timeline,
        "statuses": [
            {"key": m.message_key, "at": ist_str(m.created_at), "kind": m.kind}
            for m in call.messages if m.kind in ("status", "system")
        ],
    }


# --------------------------------------------------------------------------
# Desk actions (confirm / decline / acknowledge)
# --------------------------------------------------------------------------

def _desk_event_for_user(db: Session, user: User, event_id: int,
                         *, allow_ack: bool) -> tuple[DispatchEvent | None, DispatchCase | None, str | None]:
    """Resolve + authorise one desk action. Returns (event, case, error)."""
    ev = db.get(DispatchEvent, event_id)
    if ev is None:
        return None, None, "not_found"
    is_operator = user.role in (Role.operator.value, Role.admin.value)
    if ev.track == DispatchTrack.operator.value:
        # Follow-up queue: operators/admins only.
        if not (is_operator and allow_ack):
            return None, None, "forbidden"
        return ev, db.get(DispatchCase, ev.case_id), None
    if is_operator:
        return ev, db.get(DispatchCase, ev.case_id), None
    if user.role == Role.asha.value:
        case = db.get(DispatchCase, ev.case_id)
        call = db.get(CallSession, case.call_id) if case else None
        if (call is not None and user.region_id is not None
                and call.region_id == user.region_id):
            return ev, case, None
        return None, None, "forbidden"
    return None, None, "forbidden"


class DeskBody(BaseModel):
    note: str = ""


@router.post("/desk/{event_id}/confirm")
def desk_confirm(event_id: int, request: Request, body: DeskBody | None = None,
                 db: Session = Depends(get_db)):
    user = _require_user(request, db)
    ev, case, err = _desk_event_for_user(db, user, event_id, allow_ack=False)
    if err:
        raise HTTPException(status_code=404 if err == "not_found" else 403,
                            detail=err)
    if ev.resolved or ev.action != DispatchAction.call_placed.value:
        return {"ok": False, "error": "not_pending"}
    dispatch_service.confirm(db, case, ev.track, by=f"app:{user.username}",
                             contact_id=ev.contact_id)
    db.commit()
    return {"ok": True}


@router.post("/desk/{event_id}/decline")
def desk_decline(event_id: int, request: Request, body: DeskBody | None = None,
                 db: Session = Depends(get_db)):
    user = _require_user(request, db)
    ev, case, err = _desk_event_for_user(db, user, event_id, allow_ack=False)
    if err:
        raise HTTPException(status_code=404 if err == "not_found" else 403,
                            detail=err)
    if ev.resolved or ev.action != DispatchAction.call_placed.value:
        return {"ok": False, "error": "not_pending"}
    dispatch_service.decline(db, case, ev.track, by=f"app:{user.username}")
    db.commit()
    return {"ok": True}


@router.post("/desk/{event_id}/ack")
def desk_ack(event_id: int, request: Request, body: DeskBody | None = None,
             db: Session = Depends(get_db)):
    """Operator acknowledges a manual-follow-up alert (records that a human
    has seen it; does not stop the escalation chain)."""
    user = _require_user(request, db)
    ev, case, err = _desk_event_for_user(db, user, event_id, allow_ack=True)
    if err:
        raise HTTPException(status_code=404 if err == "not_found" else 403,
                            detail=err)
    if ev.track != DispatchTrack.operator.value:
        return {"ok": False, "error": "not_an_operator_alert"}
    ev.resolved = True
    ev.detail = (ev.detail or "") + f" | acknowledged by app:{user.username}"
    db.add(DispatchEvent(
        case_id=ev.case_id,
        track=DispatchTrack.operator.value,
        action=DispatchAction.escalated.value,
        detail=f"operator alert acknowledged by app:{user.username}",
        resolved=True,
    ))
    db.commit()
    return {"ok": True}
