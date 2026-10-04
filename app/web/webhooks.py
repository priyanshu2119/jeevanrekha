"""Production telephony webhooks (Exotel / Twilio adapters).

These endpoints are the bridge between a real PSTN call and the same
state machine the simulator drives. They are thin on purpose: all logic
lives in services.call_flow / services.dispatch so behaviour is identical
across channels and fully testable offline.

Inbound caller flow (voice webhook -> DTMF digit -> state machine):
    provider POSTs {From, To, Digits, CallSid} -> find the in-progress
    phone session for that caller number (or open one, resolving the region
    from the dialled DID) -> call_flow.handle_digit()

Responder confirmation (outbound dispatch call -> responder presses 1):
    provider POSTs {To/From of responder, Digits="1"}  ->  find the pending
    dispatch attempt for that phone number  ->  dispatch.confirm()

Security (a webhook is the only door from the public internet into the
triage/dispatch machinery -- an attacker who can POST here can fake
"ambulance confirmed" to a panicking caller):
* every request passes app.web.webhook_auth.authorize_webhook (shared
  secret and/or source-IP allowlist; Twilio requests are additionally
  signature-verified when JR_TWILIO_TOKEN is set);
* rejections are written to the incident log for audit;
* providers retry webhooks that answer slowly (Exotel: up to 2 retries), so
  an identical (CallSid, digit) pair inside JR_WEBHOOK_DEDUP seconds is
  treated as a replay and NOT recorded twice -- a duplicated digit would
  otherwise answer the NEXT question on the caller's behalf, and a
  duplicated "no" could under-escalate a real emergency.

Local development uses the simulated provider and the dispatch desk UI
instead; with no auth configured these routes stay open (zero-config local
use), but settings.validate_runtime() refuses to boot production that way.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..engine import i18n
from ..models import (
    CallSession,
    CallStatus,
    Channel,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    IncidentLog,
    RegionPhoneNumber,
    StatusMessage,
    utcnow,
)
from ..services import call_flow, dispatch as dispatch_service
from .webhook_auth import authorize_webhook

router = APIRouter(prefix="/webhooks")
log = logging.getLogger("jr.webhooks")


def _find_inbound_session(db: Session, caller_phone: str, channel_prefix: str) -> CallSession | None:
    normalized = "".join(ch for ch in (caller_phone or "") if ch.isdigit())[-10:]
    sessions = db.scalars(
        select(CallSession).where(
            CallSession.status == CallStatus.in_progress.value,
            CallSession.channel.like(f"{channel_prefix}%"),
        ).order_by(CallSession.id.desc())
    ).all()
    for s in sessions:
        s_tail = "".join(ch for ch in (s.caller_phone or "") if ch.isdigit())[-10:]
        if s_tail and s_tail == normalized:
            return s
    return None


def _resolve_region_id(db: Session, provider: str, dialled_number: str) -> int | None:
    """Map the DID the caller dialled to its region (the deployment's
    numbering plan). Without this a real phone call has no region, and no
    region means no Track B backup chain -- only the generic 108 alert."""
    normalized = "".join(ch for ch in (dialled_number or "") if ch.isdigit())[-10:]
    if not normalized:
        return None
    rows = db.scalars(
        select(RegionPhoneNumber).where(
            RegionPhoneNumber.provider == provider,
            RegionPhoneNumber.is_active.is_(True),
        )
    ).all()
    for row in rows:
        row_tail = "".join(ch for ch in row.phone_number if ch.isdigit())[-10:]
        if row_tail and row_tail == normalized:
            return row.region_id
    return None


_DEDUP_KEY = "webhook_last_input"


def _is_replayed_input(db: Session, session: CallSession, call_sid: str, digit: str) -> bool:
    """True when this exact (CallSid, digit) was already processed within the
    dedupe window -- i.e. the provider replayed its webhook. The meta row is
    part of the audit trail (kind="meta" is never rendered to callers)."""
    if not call_sid or not digit:
        return False
    now = utcnow()
    row = db.scalars(
        select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.kind == "meta",
            StatusMessage.message_key == _DEDUP_KEY,
        )
    ).first()
    if row is not None:
        params = row.params or {}
        same = params.get("sid") == call_sid and params.get("digit") == digit
        try:
            last = datetime.fromisoformat(params.get("at", ""))
        except ValueError:
            last = None
        if same and last is not None:
            age = (now - last).total_seconds()
            if 0 <= age < settings.WEBHOOK_DEDUP_SEC:
                return True
        row.params = {"sid": call_sid, "digit": digit, "at": now.isoformat()}
        row.created_at = now
    else:
        db.add(StatusMessage(
            call_id=session.id, kind="meta", message_key=_DEDUP_KEY,
            params={"sid": call_sid, "digit": digit, "at": now.isoformat()},
        ))
    db.flush()
    return False


async def _guard(request: Request, db: Session, provider: str) -> tuple[dict[str, str], Response | None]:
    """Authenticate one webhook request. Returns (form_params, error_response).
    error_response is None when the request is allowed."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    ok, reason = authorize_webhook(request, provider, form)
    if not ok:
        log.warning("webhook rejected (%s): %s %s", provider, request.url.path, reason)
        db.add(IncidentLog(kind="webhook_rejected",
                           detail=f"provider={provider} path={request.url.path} reason={reason}"))
        db.commit()
        return form, JSONResponse({"error": "forbidden", "detail": reason}, status_code=403)
    return form, None


@router.post("/exotel/inbound")
async def exotel_inbound(
    request: Request,
    From: str = Form(""),
    Digits: str = Form(""),
    CallSid: str = Form(""),
    To: str = Form(""),
    CallTo: str = Form(""),
    db: Session = Depends(get_db),
):
    _form, err = await _guard(request, db, "exotel")
    if err is not None:
        return err
    session = _find_inbound_session(db, From, "phone_exotel")
    if session is None:
        # First webhook for this caller: open a session, resolving the region
        # from the dialled DID (one helpline number per region is the most
        # reliable routing signal -- a panicking caller should never have to
        # spell out where she is).
        did = CallTo or To or _form.get("CallTo") or _form.get("To") or ""
        region_id = _resolve_region_id(db, "exotel", did)
        session = call_flow.start_call(db, channel=Channel.phone_exotel,
                                       region_id=region_id, caller_phone=From)
    if Digits:
        if _is_replayed_input(db, session, CallSid, Digits):
            log.info("ignoring replayed exotel webhook (CallSid=%s digit=%s)",
                     CallSid, Digits)
        else:
            call_flow.handle_digit(db, session, Digits)
    else:
        call_flow.handle_silence(db, session)
    db.commit()
    prompts = call_flow.opening_prompts(db, session)
    text = " ".join(i18n.t(session.language, p.key, **p.params) for p in prompts)
    # Exotel expects TwiML-like XML; TTS body served back to the call.
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{_esc(text)}</Say></Response>'
    return Response(content=xml, media_type="text/xml")


@router.post("/twilio/inbound")
async def twilio_inbound(
    request: Request,
    From: str = Form(""),
    Digits: str = Form(""),
    CallSid: str = Form(""),
    To: str = Form(""),
    db: Session = Depends(get_db),
):
    _form, err = await _guard(request, db, "twilio")
    if err is not None:
        return err
    session = _find_inbound_session(db, From, "phone_twilio")
    if session is None:
        region_id = _resolve_region_id(db, "twilio", To)
        session = call_flow.start_call(db, channel=Channel.phone_twilio,
                                       region_id=region_id, caller_phone=From)
    if Digits:
        if _is_replayed_input(db, session, CallSid, Digits):
            log.info("ignoring replayed twilio webhook (CallSid=%s digit=%s)",
                     CallSid, Digits)
        else:
            call_flow.handle_digit(db, session, Digits)
    else:
        call_flow.handle_silence(db, session)
    db.commit()
    prompts = call_flow.opening_prompts(db, session)
    text = " ".join(i18n.t(session.language, p.key, **p.params) for p in prompts)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{_esc(text)}</Say><Pause length="1"/></Response>'
    return Response(content=xml, media_type="text/xml")


@router.post("/responder-confirm")
async def responder_confirm(
    request: Request,
    phone: str = Form(...),
    digits: str = Form("1"),
    db: Session = Depends(get_db),
):
    """Responder pressed 1 on the outbound dispatch call."""
    _form, err = await _guard(request, db, settings.TELEPHONY_PROVIDER)
    if err is not None:
        return err
    normalized = "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]
    pending = db.scalars(
        select(DispatchEvent).where(
            DispatchEvent.action == DispatchAction.call_placed.value,
            DispatchEvent.resolved.is_(False),
        ).order_by(DispatchEvent.id.desc())
    ).all()
    for ev in pending:
        tail = "".join(ch for ch in (ev.contact_phone or "") if ch.isdigit())[-10:]
        if tail == normalized:
            case = db.get(DispatchCase, ev.case_id)
            if digits == "1" and case:
                dispatch_service.confirm(db, case, ev.track, by="responder-dtmf",
                                         contact_id=ev.contact_id)
            elif case:
                dispatch_service.decline(db, case, ev.track, by="responder-dtmf")
            db.commit()
            return {"ok": True, "event_id": ev.id}
    log.warning("responder-confirm for unknown phone tail %s", normalized[-4:])
    return {"ok": False, "error": "no_pending_alert"}


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
