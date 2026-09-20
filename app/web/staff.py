"""Staff web layer: login, ASHA worker view, administrator dashboard,
region & contact configuration.

Privacy posture (DPDP-2023 minded): phone numbers are masked everywhere in
staff views except the dispatch desk (which needs real numbers to ring);
the admin dashboard is aggregate and anonymised by design; ASHA workers only
ever see their own region.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..engine import i18n
from ..engine.questions import QUESTION_INDEX
from ..models import (
    Answer,
    BackupContact,
    CallSession,
    CallStatus,
    ContactKind,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    DispatchStatus,
    IncidentLog,
    Region,
    Role,
    Tier,
    TriageResult,
    User,
    utcnow,
)
from ..security import make_session_token, verify_password
from ..config import settings
from ..services.dispatch import ist_str
from .deps import get_templates, request_user

router = APIRouter()

TEMPLATES = get_templates


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/staff", error: str = ""):
    return TEMPLATES().TemplateResponse(request, "login.html", {
        "next": next, "error": error,
    })


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/staff"),
    db: Session = Depends(get_db),
):
    user = db.scalars(select(User).where(User.username == username.strip())).first()
    if user is None or not user.is_active or not verify_password(password, user):
        return RedirectResponse(f"/login?next={next}&error=1", status_code=303)
    response = RedirectResponse(next, status_code=303)
    response.set_cookie(
        settings.SESSION_COOKIE,
        make_session_token(user.id),
        max_age=12 * 3600,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
def logout(request: Request):
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(settings.SESSION_COOKIE)
    return response


@router.get("/staff")
def staff_root(request: Request, db: Session = Depends(get_db)):
    user = request_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role == Role.admin.value:
        return RedirectResponse("/admin", status_code=303)
    if user.role == Role.operator.value:
        return RedirectResponse("/sim", status_code=303)
    return RedirectResponse("/asha", status_code=303)


def _require(request: Request, db: Session, *roles: Role) -> User | None:
    user = request_user(request, db)
    if user is None or user.role not in {r.value for r in roles}:
        return None
    return user


# --------------------------------------------------------------------------
# Shared query helpers
# --------------------------------------------------------------------------

def _call_detail_ctx(db: Session, call: CallSession) -> dict:
    answers = db.scalars(
        select(Answer).where(Answer.call_id == call.id).order_by(Answer.id)
    ).all()
    answer_rows = []
    for a in answers:
        q = QUESTION_INDEX.get(a.question_id)
        answer_rows.append({
            "question_id": a.question_id,
            "question_en": i18n.t("en", f"q_{a.question_id}"),
            "severity": q.severity if q else "?",
            "value": a.value,
            "ambiguous": a.ambiguous,
            "repeats": a.repeats,
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
        events = db.scalars(
            select(DispatchEvent).where(DispatchEvent.case_id == case.id).order_by(DispatchEvent.id)
        ).all()
        for ev in events:
            timeline.append({
                "at": ist_str(ev.created_at),
                "track": ev.track,
                "action": ev.action,
                "attempt": ev.attempt,
                "contact": ev.contact_name or "—",
                "phone": ev.contact_phone or "",
                "detail": ev.detail or "",
            })
    statuses = [(m.message_key, ist_str(m.created_at), m.kind) for m in call.messages
                if m.kind in ("status", "system")]
    return {
        "call": call,
        "answers": answer_rows,
        "result": result,
        "case": case,
        "timeline": timeline,
        "statuses": statuses,
    }


# --------------------------------------------------------------------------
# ASHA worker view
# --------------------------------------------------------------------------

@router.get("/asha", response_class=HTMLResponse)
def asha_home(request: Request, db: Session = Depends(get_db)):
    user = _require(request, db, Role.asha, Role.admin)
    if user is None:
        return RedirectResponse("/login?next=/asha", status_code=303)
    region = user.region
    if region is None and user.role == Role.asha.value:
        return TEMPLATES().TemplateResponse(request, "asha.html", {
            "user": user, "region": None, "calls": [], "stats": {},
            "repeat_callers": [], "sign_counts": [],
        })

    q = select(CallSession).order_by(CallSession.id.desc())
    if region is not None and user.role == Role.asha.value:
        q = q.where(CallSession.region_id == region.id)
    calls = db.scalars(q.limit(60)).all()

    since = utcnow() - timedelta(days=30)
    region_calls = [c for c in calls if c.started_at and c.started_at >= since] if calls else []
    tier_counts = Counter(
        c.result.tier for c in calls if c.result is not None
    )

    # Repeat callers (masked): families with recurring needs are exactly the
    # pattern an ASHA should notice.
    phone_counts: Counter[str] = Counter()
    for c in calls:
        if c.caller_phone:
            phone_counts[c.caller_phone] += 1
    repeat_callers = [
        {"phone": p, "count": n, "last": max(
            (c.started_at for c in calls if c.caller_phone == p), default=None)}
        for p, n in phone_counts.items() if n >= 2
    ]
    repeat_callers.sort(key=lambda r: r["count"], reverse=True)

    # Recurring danger signs in her area (30 days).
    sign_counter: Counter[str] = Counter()
    results = db.scalars(
        select(TriageResult)
        .join(CallSession, CallSession.id == TriageResult.call_id)
        .where(
            CallSession.started_at >= since,
            *([CallSession.region_id == region.id] if region is not None and user.role == Role.asha.value else []),
        )
    ).all()
    for r in results:
        for sign in r.flagged or []:
            sign_counter[sign] += 1
    sign_counts = [{"sign": s, "count": n, "label": _sign_label(s)}
                   for s, n in sign_counter.most_common(8)]

    return TEMPLATES().TemplateResponse(request, "asha.html", {
        "user": user,
        "region": region,
        "calls": calls[:40],
        "stats": {
            "total_30d": len(region_calls),
            "emergency": tier_counts.get(Tier.emergency.value, 0),
            "urgent": tier_counts.get(Tier.urgent.value, 0),
            "reassurance": tier_counts.get(Tier.reassurance.value, 0),
        },
        "repeat_callers": repeat_callers[:8],
        "sign_counts": sign_counts,
    })


def _sign_label(sign_key: str) -> str:
    labels = {
        "bleeding": "Heavy bleeding",
        "fits": "Fits / convulsions",
        "breathing": "Severe breathing difficulty",
        "fever": "High fever",
        "headache_vision": "Severe headache / blurred vision",
        "abdominal_pain": "Severe abdominal pain",
        "fetal_movement": "Reduced fetal movement",
        "water_leaking": "Water leaking before due date",
        "swelling": "Swelling (face/hands/feet)",
        "vomiting": "Persistent vomiting",
        "not_feeding": "Baby not feeding",
        "fast_breathing": "Baby fast breathing / chest indrawing",
        "body_hot": "Baby body very hot",
        "body_cold": "Baby body cold",
        "lethargy": "Baby unusually sleepy",
        "jaundice": "Yellow palms / soles",
        "cord_infection": "Cord redness / pus",
        "loose_stools": "Watery stools / vomiting everything",
    }
    return labels.get(sign_key, sign_key)


@router.get("/asha/calls/{ref}", response_class=HTMLResponse)
def asha_call_detail(ref: str, request: Request, db: Session = Depends(get_db)):
    user = _require(request, db, Role.asha, Role.admin)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    call = db.scalars(select(CallSession).where(CallSession.ref_code == ref)).first()
    if call is None:
        return RedirectResponse("/asha", status_code=303)
    if (
        user.role == Role.asha.value
        and user.region_id is not None
        and call.region_id != user.region_id
    ):
        return RedirectResponse("/asha", status_code=303)  # scoped to her area
    ctx = _call_detail_ctx(db, call)
    ctx.update({"user": user, "back": "/asha"})
    return TEMPLATES().TemplateResponse(request, "call_detail.html", ctx)


# --------------------------------------------------------------------------
# Administrator dashboard
# --------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, days: int = 30, db: Session = Depends(get_db)):
    user = _require(request, db, Role.admin)
    if user is None:
        return RedirectResponse("/login?next=/admin", status_code=303)
    days = max(1, min(days, 365))
    since = utcnow() - timedelta(days=days)

    calls = db.scalars(
        select(CallSession).where(CallSession.started_at >= since)
    ).all()
    tier_counts = Counter(c.result.tier for c in calls if c.result)
    channel_counts = Counter(c.channel for c in calls)

    # --- the uncomfortable metric -------------------------------------------
    # Of emergency-tier calls: how many got a confirmed dispatch inside the
    # window, how many had to escalate, how many exhausted the chain.
    cases = db.scalars(
        select(DispatchCase)
        .join(CallSession, CallSession.id == DispatchCase.call_id)
        .where(CallSession.started_at >= since)
    ).all()
    confirmed_in_window = 0
    escalated = 0
    exhausted = 0
    confirm_deltas: list[float] = []
    for case in cases:
        timeouts = db.scalars(
            select(func.count(DispatchEvent.id)).where(
                DispatchEvent.case_id == case.id,
                DispatchEvent.action == DispatchAction.timeout.value,
            )
        ).one()
        if timeouts:
            escalated += 1
        if case.status == DispatchStatus.confirmed.value and case.confirmed_at:
            delta = (case.confirmed_at - case.started_at).total_seconds()
            confirm_deltas.append(delta)
            window = settings.DISPATCH_CONFIRM_WINDOW_SEC
            if delta <= window:
                confirmed_in_window += 1
        elif case.status == DispatchStatus.exhausted.value:
            exhausted += 1

    total_cases = len(cases)
    median_confirm = (
        sorted(confirm_deltas)[len(confirm_deltas) // 2] if confirm_deltas else None
    )

    # Per-region rollup.
    regions = db.scalars(select(Region).order_by(Region.state, Region.district)).all()
    region_rows = []
    for r in regions:
        rcalls = [c for c in calls if c.region_id == r.id]
        rtiers = Counter(c.result.tier for c in rcalls if c.result)
        rcases = db.scalars(
            select(DispatchCase)
            .join(CallSession, CallSession.id == DispatchCase.call_id)
            .where(CallSession.region_id == r.id, CallSession.started_at >= since)
        ).all()
        r_conf = sum(1 for c in rcases if c.status == DispatchStatus.confirmed.value)
        r_exh = sum(1 for c in rcases if c.status == DispatchStatus.exhausted.value)
        region_rows.append({
            "region": r,
            "calls": len(rcalls),
            "emergency": rtiers.get(Tier.emergency.value, 0),
            "urgent": rtiers.get(Tier.urgent.value, 0),
            "reassurance": rtiers.get(Tier.reassurance.value, 0),
            "dispatch_cases": len(rcases),
            "confirmed": r_conf,
            "exhausted": r_exh,
        })

    incidents = db.scalars(
        select(IncidentLog).order_by(IncidentLog.id.desc()).limit(15)
    ).all()

    recent_emergencies = db.scalars(
        select(CallSession)
        .join(TriageResult, TriageResult.call_id == CallSession.id)
        .where(TriageResult.tier == Tier.emergency.value)
        .order_by(CallSession.id.desc())
        .limit(10)
    ).all()

    return TEMPLATES().TemplateResponse(request, "admin.html", {
        "user": user,
        "days": days,
        "totals": {
            "calls": len(calls),
            "emergency": tier_counts.get(Tier.emergency.value, 0),
            "urgent": tier_counts.get(Tier.urgent.value, 0),
            "reassurance": tier_counts.get(Tier.reassurance.value, 0),
            "abandoned": sum(1 for c in calls if c.status == CallStatus.abandoned.value),
        },
        "channels": dict(channel_counts),
        "dispatch": {
            "total_cases": total_cases,
            "confirmed_in_window": confirmed_in_window,
            "escalated": escalated,
            "exhausted": exhausted,
            "median_confirm_sec": median_confirm,
            "confirm_rate": round(100 * confirmed_in_window / total_cases) if total_cases else None,
        },
        "region_rows": region_rows,
        "incidents": incidents,
        "recent_emergencies": recent_emergencies,
        "tier_counts": dict(tier_counts),
    })


@router.get("/admin/calls", response_class=HTMLResponse)
def admin_calls(
    request: Request,
    tier: str = "",
    region_id: int = 0,
    page: int = 1,
    db: Session = Depends(get_db),
):
    user = _require(request, db, Role.admin)
    if user is None:
        return RedirectResponse("/login?next=/admin/calls", status_code=303)
    q = select(CallSession).order_by(CallSession.id.desc())
    if tier:
        q = q.join(TriageResult, TriageResult.call_id == CallSession.id).where(
            TriageResult.tier == tier
        )
    if region_id:
        q = q.where(CallSession.region_id == region_id)
    total = db.scalars(select(func.count()).select_from(q.subquery())).one()
    per_page = 40
    calls = db.scalars(q.offset((max(page, 1) - 1) * per_page).limit(per_page)).all()
    regions = db.scalars(select(Region).order_by(Region.state)).all()
    return TEMPLATES().TemplateResponse(request, "admin_calls.html", {
        "user": user, "calls": calls, "regions": regions,
        "tier": tier, "region_id": region_id, "page": page,
        "pages": max(1, (total + per_page - 1) // per_page), "total": total,
    })


@router.get("/admin/calls/{ref}", response_class=HTMLResponse)
def admin_call_detail(ref: str, request: Request, db: Session = Depends(get_db)):
    user = _require(request, db, Role.admin)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    call = db.scalars(select(CallSession).where(CallSession.ref_code == ref)).first()
    if call is None:
        return RedirectResponse("/admin/calls", status_code=303)
    ctx = _call_detail_ctx(db, call)
    ctx.update({"user": user, "back": "/admin/calls"})
    return TEMPLATES().TemplateResponse(request, "call_detail.html", ctx)


# --------------------------------------------------------------------------
# Region & contact configuration (admin)
# --------------------------------------------------------------------------

@router.get("/admin/regions", response_class=HTMLResponse)
def admin_regions(request: Request, db: Session = Depends(get_db)):
    user = _require(request, db, Role.admin)
    if user is None:
        return RedirectResponse("/login?next=/admin/regions", status_code=303)
    regions = db.scalars(select(Region).order_by(Region.state, Region.district, Region.block)).all()
    kinds = [k.value for k in ContactKind]
    return TEMPLATES().TemplateResponse(request, "admin_regions.html", {
        "user": user, "regions": regions, "kinds": kinds,
        "default_window": settings.DISPATCH_CONFIRM_WINDOW_SEC,
    })


@router.post("/admin/regions")
def admin_region_create(
    request: Request,
    state: str = Form(...),
    district: str = Form(...),
    block: str = Form(...),
    emergency_number: str = Form("108"),
    confirm_window_sec: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require(request, db, Role.admin)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    window = int(confirm_window_sec) if confirm_window_sec.strip().isdigit() else None
    db.add(Region(
        state=state.strip(), district=district.strip(), block=block.strip(),
        emergency_number=emergency_number.strip() or "108",
        confirm_window_sec=window,
    ))
    db.commit()
    return RedirectResponse("/admin/regions", status_code=303)


@router.post("/admin/regions/{region_id}")
def admin_region_update(
    region_id: int,
    request: Request,
    emergency_number: str = Form(...),
    confirm_window_sec: str = Form(""),
    is_active: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require(request, db, Role.admin)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    region = db.get(Region, region_id)
    if region is None:
        return RedirectResponse("/admin/regions", status_code=303)
    region.emergency_number = emergency_number.strip() or "108"
    region.confirm_window_sec = (
        int(confirm_window_sec) if confirm_window_sec.strip().isdigit() else None
    )
    region.is_active = is_active == "on"
    db.commit()
    return RedirectResponse("/admin/regions", status_code=303)


@router.post("/admin/regions/{region_id}/contacts")
def admin_contact_create(
    region_id: int,
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),
    phone: str = Form(...),
    priority: str = Form("100"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require(request, db, Role.admin)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    region = db.get(Region, region_id)
    if region is None:
        return RedirectResponse("/admin/regions", status_code=303)
    db.add(BackupContact(
        region_id=region.id,
        name=name.strip(),
        kind=kind if kind in {k.value for k in ContactKind} else ContactKind.asha.value,
        phone=phone.strip(),
        priority=int(priority) if priority.strip().isdigit() else 100,
        notes=notes.strip() or None,
    ))
    db.commit()
    return RedirectResponse("/admin/regions", status_code=303)


@router.post("/admin/contacts/{contact_id}")
def admin_contact_update(
    contact_id: int,
    request: Request,
    phone: str = Form(...),
    priority: str = Form("100"),
    is_active: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require(request, db, Role.admin)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    contact = db.get(BackupContact, contact_id)
    if contact is None:
        return RedirectResponse("/admin/regions", status_code=303)
    contact.phone = phone.strip() or contact.phone
    contact.priority = int(priority) if priority.strip().isdigit() else contact.priority
    contact.is_active = is_active == "on"
    db.commit()
    return RedirectResponse("/admin/regions", status_code=303)
