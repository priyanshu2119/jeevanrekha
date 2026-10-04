"""Production telephony webhooks (Exotel ExoML / Twilio TwiML).

These endpoints ARE the phone call. A real PSTN leg is driven entirely by
the XML we return here, and every verb is rendered by services.voice_xml
from the same state machine the simulator drives -- so a call from an actual
handset behaves identically to the /sim console, and all of it stays
testable offline.

Inbound caller flow (ExoML Gather loop):
    caller dials the ExoPhone -> Exotel fetches /webhooks/exotel/inbound
    -> we return <Gather action=INBOUND><Say>prompt</Say></Gather>
       <Redirect>INBOUND</Redirect>
    -> caller presses 1/2/3: Exotel POSTs Digits to the action URL
    -> caller stays silent: execution falls through the Gather into the
       Redirect, which arrives WITHOUT Digits = silence (repeat once, then
       UNCLEAR -> escalate, per the flow rules)
    -> on the emergency tier the caller is held in an honest status loop
       (latest known status + "still waiting") until a human confirms, the
       chain exhausts, or a bounded number of loops passes -- after which we
       say goodbye and HANG UP while dispatch keeps running server-side.

Outbound responder alert:
    dispatch places Calls/connect with Url=/webhooks/{p}/responder-alert
    ?event_id=N -> responder answers -> we serve <Gather action=
    /webhooks/responder-confirm?event_id=N><Say>alert</Say></Gather>
    -> press 1 = confirmed, press 2 = decline-and-escalate, anything else
       is ignored (a mispress must never be read as a decline).
    StatusCallback -> /webhooks/{p}/call-status?event_id=N: busy/no-answer/
    failed escalates IMMEDIATELY (dispatch.attempt_failed) instead of
    burning the confirmation window on a line nobody answered.

Inbound call teardown:
    a terminal status callback with Direction=inbound finalises the caller's
    session exactly like a hangup on the simulator (partial answers ->
    missing = unclear = escalate; an emergency dispatch already running
    stays running).

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

from fastapi import APIRouter, Depends, Request
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
    DispatchStatus,
    IncidentLog,
    RegionPhoneNumber,
    StatusMessage,
    Tier,
    utcnow,
)
from ..services import call_flow, dispatch as dispatch_service, voice_xml
from ..services.telephony import webhook_url
from . import serializers
from .webhook_auth import authorize_webhook

router = APIRouter(prefix="/webhooks")
log = logging.getLogger("jr.webhooks")

# Provider parameter names are inconsistent across products and transports
# (Exotel Passthru sends CallFrom/CallTo and lowercase digits; ExoML Gather
# and Twilio send From/To/Digits; Twilio status callbacks send CallStatus).
# Canonicalise once, defensively.
_PARAM_ALIASES = {
    "call_sid": ("CallSid", "callSid", "CallSID", "call_sid"),
    "from_phone": ("From", "CallFrom", "from"),
    "to_phone": ("To", "CallTo", "to"),
    "digits": ("Digits", "digits"),
    "status": ("Status", "status", "CallStatus", "callStatus"),
    "direction": ("Direction", "direction"),
}

# Terminal provider call statuses (Exotel constants per goexoml; Twilio uses
# the same vocabulary). Negative terminals on an outbound alert escalate
# immediately; "completed" without a keypress does NOT -- the responder may
# already be moving, and the confirmation window still governs.
_NEGATIVE_TERMINAL = {"failed", "busy", "no-answer", "cancelled", "canceled"}
_TERMINAL = _NEGATIVE_TERMINAL | {"completed"}

# Bound on the emergency status loop (~20s per iteration => ~10 min on the
# line). After this we say goodbye honestly; dispatch continues server-side.
MAX_STATUS_LOOPS = 30
_LOOP_KEY = "ivr_status_loops"


async def _merged_params(request: Request) -> dict[str, str]:
    """Query + form parameters, canonicalised through _PARAM_ALIASES.

    Our own ExoML action URLs carry CallSid/From/To in the query string (so
    identity survives even if a provider forwards only the Digits body),
    while provider fields arrive in the form body. Form values win.
    """
    raw: dict[str, str] = {k: str(v) for k, v in request.query_params.items()}
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            form = await request.form()
            raw.update({k: str(v) for k, v in form.items()})
        except Exception:  # noqa: BLE001 - non-form body; query params still apply
            pass
    out: dict[str, str] = {}
    for canon, names in _PARAM_ALIASES.items():
        for name in names:
            value = raw.get(name)
            if value:
                out[canon] = value
                break
    return out


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
    try:
        form = {k: str(v) for k, v in (await request.form()).items()}
    except Exception:  # noqa: BLE001 - GET or non-form body: nothing signed anyway
        form = {}
    ok, reason = authorize_webhook(request, provider, form)
    if not ok:
        log.warning("webhook rejected (%s): %s %s", provider, request.url.path, reason)
        db.add(IncidentLog(kind="webhook_rejected",
                           detail=f"provider={provider} path={request.url.path} reason={reason}"))
        db.commit()
        return form, JSONResponse({"error": "forbidden", "detail": reason}, status_code=403)
    return form, None


# --------------------------------------------------------------------------
# Session resolution
# --------------------------------------------------------------------------

def _get_or_open_session(db: Session, params: dict[str, str], provider: str,
                         channel: Channel) -> CallSession:
    """Find the live session for this call leg, or open one.

    Lookup order: exact provider CallSid (stored on the session by the first
    webhook of the call) -> caller phone tail (first webhook only). The
    region comes from the dialled DID (the deployment's numbering plan):
    no region would mean no Track B backup chain, so DID mapping is what
    makes parallel routing real for phone callers.
    """
    call_sid = params.get("call_sid", "")
    from_phone = params.get("from_phone", "")
    session: CallSession | None = None
    if call_sid:
        session = db.scalars(
            select(CallSession).where(
                CallSession.provider_call_sid == call_sid,
                CallSession.channel == channel.value,
            ).order_by(CallSession.id.desc())
        ).first()
    if session is None:
        session = _find_inbound_session(db, from_phone, channel.value)
    if session is None:
        region_id = _resolve_region_id(db, provider, params.get("to_phone", ""))
        session = call_flow.start_call(db, channel=channel,
                                       region_id=region_id,
                                       caller_phone=from_phone or None)
    if call_sid and session.provider_call_sid != call_sid:
        # Bind (or rebind after a provider-side leg change) the exact key.
        session.provider_call_sid = call_sid
        db.flush()
    return session


# --------------------------------------------------------------------------
# Voice response rendering (ExoML/TwiML -- one verb set serves both)
# --------------------------------------------------------------------------

def _render_prompts(session: CallSession, prompts) -> str:
    return " ".join(i18n.t(session.language, p.key, **p.params) for p in prompts)


def _inbound_url(provider: str, params: dict[str, str]) -> str:
    """Our own inbound webhook, carrying the call identity in the query so
    both the Gather action POST and the silence Redirect resolve the session
    even if a provider forwards only minimal body parameters."""
    return webhook_url(
        f"/webhooks/{provider}/inbound",
        CallSid=params.get("call_sid", ""),
        From=params.get("from_phone", ""),
        To=params.get("to_phone", ""),
    )


def _latest_status_texts(db: Session, session: CallSession, limit: int = 2) -> list[str]:
    rows = db.scalars(
        select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.kind == "status",
        ).order_by(StatusMessage.id.desc()).limit(limit)
    ).all()
    return [i18n.t(session.language, m.message_key, **(m.params or {}))
            for m in reversed(rows)]


def _bump_status_loop(db: Session, session: CallSession) -> int:
    row = db.scalars(
        select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.kind == "meta",
            StatusMessage.message_key == _LOOP_KEY,
        )
    ).first()
    if row is None:
        db.add(StatusMessage(call_id=session.id, kind="meta",
                             message_key=_LOOP_KEY, params={"count": 1}))
        db.flush()
        return 1
    count = int((row.params or {}).get("count", 0)) + 1
    row.params = {**(row.params or {}), "count": count}
    db.flush()
    return count


def _voice_response(db: Session, session: CallSession, provider: str,
                    params: dict[str, str], step) -> str:
    """Render what the caller hears next, as provider voice XML.

    Interactive states become a Gather+Redirect pair: a keypress POSTs to
    the action URL with Digits; silence falls through the Gather into the
    Redirect, which arrives WITHOUT Digits and is recorded as silence
    (repeat once, then UNCLEAR -> escalate). The emergency tier holds the
    caller in an honest status loop until the case settles or a bounded
    number of iterations passes; everything else plays and hangs up.
    """
    state = call_flow.get_state(db, session)
    lang = session.language

    if state == call_flow.S_ENDED:
        return voice_xml.ivr_final(i18n.t(lang, "ivr_goodbye"), language=lang)

    if state == call_flow.S_RESULT:
        result = session.result
        if result is None or result.tier != Tier.emergency.value:
            prompts = step.prompts if (step is not None and step.prompts) \
                else call_flow.opening_prompts(db, session)
            return voice_xml.ivr_final(_render_prompts(session, prompts),
                                       language=lang)

        # --- emergency: honest live-status loop -----------------------------
        summary = serializers.dispatch_summary(db, session)
        case_status = (summary or {}).get("status")
        if case_status in (DispatchStatus.confirmed.value,
                           DispatchStatus.exhausted.value,
                           DispatchStatus.cancelled.value):
            texts = _latest_status_texts(db, session, limit=3)
            texts.append(i18n.t(lang, "ivr_ref", ref=session.ref_code))
            texts.append(i18n.t(lang, "ivr_goodbye"))
            return voice_xml.ivr_final(" ".join(texts), language=lang)

        loops = _bump_status_loop(db, session)
        if loops > MAX_STATUS_LOOPS:
            # Bounded hold (~10 min on the line). Dispatch keeps running
            # server-side regardless of this call leg, and nothing here
            # pretends help is confirmed when it is not.
            text = " ".join([
                i18n.t(lang, "st_waiting"),
                i18n.t(lang, "ivr_ref", ref=session.ref_code),
                i18n.t(lang, "ivr_goodbye"),
            ])
            return voice_xml.ivr_final(text, language=lang)

        if step is not None and step.prompts:
            # Triage just completed (or caller pressed a key): speak the
            # result/guidance or the fresh waiting status once.
            text = _render_prompts(session, step.prompts)
        else:
            texts = _latest_status_texts(db, session, limit=2)
            texts.append(i18n.t(lang, "st_waiting"))
            text = " ".join(texts)
        url = _inbound_url(provider, params)
        return voice_xml.ivr_gather(text, url, url, language=lang, timeout=20)

    # --- language / track / questions: one Gather per step -------------------
    prompts = step.prompts if (step is not None and step.prompts) \
        else call_flow.opening_prompts(db, session)
    url = _inbound_url(provider, params)
    return voice_xml.ivr_gather(_render_prompts(session, prompts), url, url,
                                language=lang, timeout=10)


async def _handle_inbound(request: Request, db: Session, provider: str,
                          channel: Channel) -> Response:
    params = await _merged_params(request)
    session = _get_or_open_session(db, params, provider, channel)
    digits = params.get("digits", "")
    call_sid = params.get("call_sid", "")
    step = None
    if digits:
        if _is_replayed_input(db, session, call_sid, digits):
            log.info("ignoring replayed %s webhook (CallSid=%s digit=%s)",
                     provider, call_sid, digits)
        else:
            step = call_flow.handle_digit(db, session, digits)
    else:
        step = call_flow.handle_silence(db, session)
    db.flush()
    xml = _voice_response(db, session, provider, params, step)
    db.commit()
    return Response(content=xml, media_type="text/xml")


@router.api_route("/exotel/inbound", methods=["GET", "POST"])
async def exotel_inbound(request: Request, db: Session = Depends(get_db)):
    _form, err = await _guard(request, db, "exotel")
    if err is not None:
        return err
    return await _handle_inbound(request, db, "exotel", Channel.phone_exotel)


@router.api_route("/twilio/inbound", methods=["GET", "POST"])
async def twilio_inbound(request: Request, db: Session = Depends(get_db)):
    _form, err = await _guard(request, db, "twilio")
    if err is not None:
        return err
    return await _handle_inbound(request, db, "twilio", Channel.phone_twilio)


# --------------------------------------------------------------------------
# Outbound responder alert (ExoML/TwiML served to the dispatch call leg)
# --------------------------------------------------------------------------

def _responder_alert_xml(db: Session, event_id: str) -> str:
    # event_id is parsed defensively: providers fetch this URL verbatim, and
    # anything but valid voice XML on a live call leg drops the responder
    # into a provider error path -- so garbage in, polite Hangup out.
    ev = db.get(DispatchEvent, int(event_id)) if str(event_id).isdigit() else None
    if ev is None:
        return voice_xml.ivr_final(
            "This JeevanRekha alert is no longer active. No action is needed.")
    case = db.get(DispatchCase, ev.case_id)
    session = db.get(CallSession, case.call_id) if case else None
    if (ev.resolved or case is None or session is None
            or case.status != DispatchStatus.active.value):
        # Stood down / confirmed elsewhere / cancelled: never leave a
        # responder guessing on a live line.
        return voice_xml.ivr_final(
            "This JeevanRekha alert has already been resolved. No action is "
            "needed. Thank you.")
    text = dispatch_service.alert_message_text(session)
    if ev.contact_name:
        text = f"Alert for {ev.contact_name}. {text}"
    action = webhook_url("/webhooks/responder-confirm", event_id=ev.id)
    return voice_xml.response(
        voice_xml.gather(voice_xml.say(text, language="en-IN"), action,
                         timeout=10, num_digits=1),
        voice_xml.hangup(),
    )


@router.api_route("/exotel/responder-alert", methods=["GET", "POST"])
async def exotel_responder_alert(request: Request, event_id: str = "",
                                 db: Session = Depends(get_db)):
    _form, err = await _guard(request, db, "exotel")
    if err is not None:
        return err
    return Response(content=_responder_alert_xml(db, event_id),
                    media_type="text/xml")


@router.api_route("/twilio/responder-alert", methods=["GET", "POST"])
async def twilio_responder_alert(request: Request, event_id: str = "",
                                 db: Session = Depends(get_db)):
    _form, err = await _guard(request, db, "twilio")
    if err is not None:
        return err
    return Response(content=_responder_alert_xml(db, event_id),
                    media_type="text/xml")


# --------------------------------------------------------------------------
# Responder DTMF decision
# --------------------------------------------------------------------------

@router.api_route("/responder-confirm", methods=["GET", "POST"])
async def responder_confirm(request: Request, db: Session = Depends(get_db)):
    """Responder pressed a key on the outbound dispatch call.

    1 = confirmed (help is moving), 2 = cannot respond (escalate at once,
    no retry). Any other key is IGNORED: a mispress must never be read as a
    decline -- the confirmation window keeps running and the scheduler
    escalates on timeout. The alert is targeted by the exact event id from
    the Gather action URL; phone-tail matching is only the fallback for
    provider setups that drop query strings.
    """
    form, err = await _guard(request, db, settings.TELEPHONY_PROVIDER)
    if err is not None:
        return err
    params = await _merged_params(request)
    digits = params.get("digits", "")
    event_id = request.query_params.get("event_id") or form.get("event_id") or ""
    phone = form.get("phone") or params.get("from_phone") or ""

    ev: DispatchEvent | None = None
    if event_id.isdigit():
        candidate = db.get(DispatchEvent, int(event_id))
        if (candidate is not None and not candidate.resolved
                and candidate.action == DispatchAction.call_placed.value):
            ev = candidate
    if ev is None and phone:
        normalized = "".join(ch for ch in phone if ch.isdigit())[-10:]
        pending = db.scalars(
            select(DispatchEvent).where(
                DispatchEvent.action == DispatchAction.call_placed.value,
                DispatchEvent.resolved.is_(False),
            ).order_by(DispatchEvent.id.desc())
        ).all()
        for candidate in pending:
            tail = "".join(ch for ch in (candidate.contact_phone or "")
                           if ch.isdigit())[-10:]
            if tail and tail == normalized:
                ev = candidate
                break
    if ev is None:
        log.warning("responder-confirm for unknown alert (event_id=%s phone tail=%s)",
                    event_id, "".join(ch for ch in phone if ch.isdigit())[-4:])
        return JSONResponse({"ok": False, "error": "no_pending_alert"},
                            status_code=404)

    case = db.get(DispatchCase, ev.case_id)
    if case is None:
        return JSONResponse({"ok": False, "error": "no_case"}, status_code=404)
    if digits == "1":
        dispatch_service.confirm(db, case, ev.track, by="responder-dtmf",
                                 contact_id=ev.contact_id)
    elif digits == "2":
        dispatch_service.decline(db, case, ev.track, by="responder-dtmf")
    else:
        return {"ok": False, "error": "invalid_digit"}
    db.commit()
    return {"ok": True, "event_id": ev.id}


# --------------------------------------------------------------------------
# Provider status callbacks (call teardown + failed outbound legs)
# --------------------------------------------------------------------------

async def _handle_call_status(request: Request, db: Session,
                              channel: Channel) -> JSONResponse:
    params = await _merged_params(request)
    status = (params.get("status") or "").lower()
    event_id = request.query_params.get("event_id") or ""
    changed = False

    if event_id.isdigit():
        # Outbound alert leg: a call that never connected escalates NOW --
        # waiting out the window on a dead line costs minutes nobody has.
        ev = db.get(DispatchEvent, int(event_id))
        if ev is not None and status:
            if status in _NEGATIVE_TERMINAL:
                changed = dispatch_service.attempt_failed(db, ev, status)
            elif status == "completed" and not ev.resolved:
                ev.detail = (ev.detail or "") + \
                    " | provider status: completed (awaiting confirmation)"
                changed = True
    else:
        # Inbound caller leg ended: finalise exactly like a simulator hangup
        # (partial answers -> missing = unclear = escalate; an emergency
        # dispatch already running keeps running without the caller).
        call_sid = params.get("call_sid", "")
        direction = (params.get("direction") or "").lower()
        if call_sid and status in _TERMINAL and direction != "outbound-api":
            session = db.scalars(
                select(CallSession).where(
                    CallSession.provider_call_sid == call_sid,
                    CallSession.channel == channel.value,
                ).order_by(CallSession.id.desc())
            ).first()
            if (session is not None
                    and session.status == CallStatus.in_progress.value):
                call_flow.hangup(db, session)
                changed = True
    if changed:
        db.commit()
    return JSONResponse({"ok": True})


@router.api_route("/exotel/call-status", methods=["GET", "POST"])
async def exotel_call_status(request: Request, db: Session = Depends(get_db)):
    _form, err = await _guard(request, db, "exotel")
    if err is not None:
        return err
    return await _handle_call_status(request, db, Channel.phone_exotel)


@router.api_route("/twilio/call-status", methods=["GET", "POST"])
async def twilio_call_status(request: Request, db: Session = Depends(get_db)):
    _form, err = await _guard(request, db, "twilio")
    if err is not None:
        return err
    return await _handle_call_status(request, db, Channel.phone_twilio)
