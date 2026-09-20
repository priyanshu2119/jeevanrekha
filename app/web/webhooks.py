"""Production telephony webhooks (Exotel / Twilio adapters).

These endpoints are the bridge between a real PSTN call and the same
state machine the simulator drives. They are thin on purpose: all logic
lives in services.call_flow / services.dispatch so behaviour is identical
across channels and fully testable offline.

Inbound caller flow (voice webhook -> DTMF digit -> state machine):
    provider POSTs {From, Digits, CallSid}  ->  find the in-progress
    phone session for that caller number  ->  call_flow.handle_digit()

Responder confirmation (outbound dispatch call -> responder presses 1):
    provider POSTs {To/From of responder, Digits="1"}  ->  find the pending
    dispatch attempt for that phone number  ->  dispatch.confirm()

Local development uses the simulated provider and the dispatch desk UI
instead; these routes activate when JR_TELEPHONY=exotel|twilio and the
matching credentials are configured.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..engine import i18n
from ..models import (
    CallSession,
    CallStatus,
    Channel,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
)
from ..services import call_flow, dispatch as dispatch_service

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


@router.post("/exotel/inbound")
async def exotel_inbound(
    request: Request,
    From: str = Form(""),
    Digits: str = Form(""),
    CallSid: str = Form(""),
    db: Session = Depends(get_db),
):
    session = _find_inbound_session(db, From, "phone_exotel")
    if session is None:
        # First webhook for this caller: open a session (region resolution by
        # caller-number prefix mapping belongs to the deployment's numbering
        # plan; until then the flow asks for the area code).
        session = call_flow.start_call(db, channel=Channel.phone_exotel,
                                       region_id=None, caller_phone=From)
    if Digits:
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
    db: Session = Depends(get_db),
):
    session = _find_inbound_session(db, From, "phone_twilio")
    if session is None:
        session = call_flow.start_call(db, channel=Channel.phone_twilio,
                                       region_id=None, caller_phone=From)
    if Digits:
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
