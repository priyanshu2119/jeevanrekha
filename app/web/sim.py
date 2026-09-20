"""Phone-call simulator and dispatch desk.

The simulator is the local, fully functional equivalent of the PSTN path:
it drives the exact same state machine through DTMF digits, and the dispatch
desk lets a human play the role of every responder the system contacts
(ambulance node, ASHA, PHC, local transport, district control room).
Confirmations and declines entered here flow through the same service
functions a real provider webhook would call in production.

Access: open while the configured provider is ``simulated`` (local testing);
requires an operator/admin login once a real provider is configured.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import (
    CallSession,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    DispatchTrack,
    Region,
    Role,
)
from ..services import dispatch as dispatch_service
from .deps import get_templates, request_user
from .serializers import ist_str

router = APIRouter()


def _sim_open() -> bool:
    return settings.TELEPHONY_PROVIDER == "simulated"


def _desk_allowed(request: Request, db: Session) -> bool:
    if _sim_open():
        return True
    user = request_user(request, db)
    return user is not None and user.role in (Role.operator.value, Role.admin.value)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@router.get("/sim", response_class=HTMLResponse)
def sim_page(request: Request, db: Session = Depends(get_db)):
    if not _desk_allowed(request, db):
        return RedirectResponse("/login?next=/sim", status_code=303)
    regions = db.scalars(
        select(Region).where(Region.is_active.is_(True)).order_by(Region.state, Region.district)
    ).all()
    recent = db.scalars(
        select(CallSession)
        .where(CallSession.channel == "phone_sim")
        .order_by(CallSession.id.desc())
        .limit(8)
    ).all()
    return get_templates().TemplateResponse(request, "sim.html", {
        "regions": regions,
        "recent_calls": recent,
        "provider": settings.TELEPHONY_PROVIDER,
        "confirm_window": settings.DISPATCH_CONFIRM_WINDOW_SEC,
    })


# --------------------------------------------------------------------------
# Dispatch desk API
# --------------------------------------------------------------------------

@router.get("/api/sim/desk")
def desk(db: Session = Depends(get_db)):
    """Everything currently waiting for a human on the responder side."""
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
        alerts.append({
            "event_id": ev.id,
            "case_id": ev.case_id,
            "call_ref": call.ref_code if call else "?",
            "track": ev.track,
            "contact_name": ev.contact_name,
            "contact_phone": ev.contact_phone,
            "contact_kind": ev.contact_kind,
            "attempt": ev.attempt,
            "due_at": ist_str(ev.due_at),
            "region": call.region.label if call and call.region else "—",
        })

    operator_items = db.scalars(
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
    ops = []
    for ev in operator_items:
        case = db.get(DispatchCase, ev.case_id)
        call = db.get(CallSession, case.call_id) if case else None
        ops.append({
            "event_id": ev.id,
            "call_ref": call.ref_code if call else "?",
            "action": ev.action,
            "detail": ev.detail,
            "at": ist_str(ev.created_at),
        })

    return {"alerts": alerts, "operator_alerts": ops, "simulated": _sim_open()}


class DeskAction(BaseModel):
    by: str = "dispatch-desk"


@router.post("/api/sim/desk/{event_id}/confirm")
def desk_confirm(event_id: int, body: DeskAction, request: Request, db: Session = Depends(get_db)):
    if not _desk_allowed(request, db):
        return {"error": "forbidden"}
    ev = db.get(DispatchEvent, event_id)
    if ev is None or ev.resolved or ev.action != DispatchAction.call_placed.value:
        return {"error": "not_pending"}
    case = db.get(DispatchCase, ev.case_id)
    dispatch_service.confirm(db, case, ev.track, by=body.by, contact_id=ev.contact_id)
    db.commit()
    return {"ok": True}


@router.post("/api/sim/desk/{event_id}/decline")
def desk_decline(event_id: int, body: DeskAction, request: Request, db: Session = Depends(get_db)):
    if not _desk_allowed(request, db):
        return {"error": "forbidden"}
    ev = db.get(DispatchEvent, event_id)
    if ev is None or ev.resolved or ev.action != DispatchAction.call_placed.value:
        return {"error": "not_pending"}
    case = db.get(DispatchCase, ev.case_id)
    dispatch_service.decline(db, case, ev.track, by=body.by)
    db.commit()
    return {"ok": True}


@router.post("/api/sim/desk/{event_id}/ack")
def desk_ack(event_id: int, body: DeskAction, request: Request, db: Session = Depends(get_db)):
    """Operator acknowledges a manual-follow-up alert (does not stop the
    escalation chain; it records that a human has seen it)."""
    if not _desk_allowed(request, db):
        return {"error": "forbidden"}
    ev = db.get(DispatchEvent, event_id)
    if ev is None:
        return {"error": "not_found"}
    ev.resolved = True
    ev.detail = (ev.detail or "") + f" | acknowledged by {body.by}"
    db.add(DispatchEvent(
        case_id=ev.case_id,
        track=DispatchTrack.operator.value,
        action=DispatchAction.escalated.value,
        detail=f"operator alert acknowledged by {body.by}",
        resolved=True,
    ))
    db.commit()
    return {"ok": True}
