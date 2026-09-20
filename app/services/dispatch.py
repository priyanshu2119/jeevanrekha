"""Parallel emergency routing and auto-escalation.

Why this module exists in this shape: documented audits of India's 108
ambulance system show it transports well under half of real obstetric
emergies in most states studied, with call-to-hospital times frequently
exceeding the golden hour. A single-channel "we called the ambulance" design
would therefore be a false sense of security. So:

* Track A (ambulance) and Track B (local backup chain) start SIMULTANEOUSLY.
* Every attempt is an immutable event row with a due time.
* A background worker resolves due attempts: retry, then escalate to the
  next contact in the chain, then raise a live operator alert. The system
  never goes silent after the first action.
* Caller-facing status messages state only what is known: "requested, NOT
  yet confirmed" until a human confirms.
* First confirmation on either track stands the other track's pending
  attempts down (logged, never deleted).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..engine import i18n
from ..models import (
    BackupContact,
    CallSession,
    ContactKind,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    DispatchStatus,
    DispatchTrack,
    IncidentLog,
    StatusMessage,
    utcnow,
)
from .telephony import get_provider

log = logging.getLogger("jr.dispatch")

IST = timezone(timedelta(hours=5, minutes=30))


def ist_str(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%H:%M IST")


def _window_sec(session: CallSession) -> int:
    if session.region and session.region.confirm_window_sec:
        return session.region.confirm_window_sec
    return settings.DISPATCH_CONFIRM_WINDOW_SEC


def _say(db: Session, session: CallSession, key: str, **params) -> None:
    """Append a caller-facing status message.

    NOTE: ``params`` are i18n interpolation values only -- the message kind is
    fixed to "status" here. (An earlier signature took ``kind`` as a named
    argument, which silently swallowed the {kind} placeholder of
    st_backup_requested. Renamed to avoid the collision for good.)
    """
    db.add(StatusMessage(call_id=session.id, kind="status", message_key=key, params=params))


def _add_event(
    db: Session,
    case: DispatchCase,
    *,
    track: str,
    action: str,
    attempt: int = 1,
    contact: BackupContact | None = None,
    contact_id: int | None = None,
    contact_name: str | None = None,
    contact_phone: str | None = None,
    contact_kind: str | None = None,
    detail: str | None = None,
    due_in_sec: int | None = None,
    resolved: bool | None = None,
) -> DispatchEvent:
    ev = DispatchEvent(
        case_id=case.id,
        track=track,
        action=action,
        attempt=attempt,
        contact_id=contact_id or (contact.id if contact else None),
        contact_name=contact_name or (contact.name if contact else None),
        contact_phone=contact_phone or (contact.phone if contact else None),
        contact_kind=contact_kind or (contact.kind if contact else None),
        detail=detail,
        due_at=utcnow() + timedelta(seconds=due_in_sec) if due_in_sec else None,
        # Alerts with a confirmation window stay open until resolved;
        # bookkeeping events close immediately unless told otherwise
        # (operator alerts must stay open until a human acknowledges).
        resolved=(due_in_sec is None) if resolved is None else resolved,
    )
    db.add(ev)
    return ev


def _place_alert(
    db: Session,
    case: DispatchCase,
    session: CallSession,
    *,
    track: str,
    attempt: int,
    contact: BackupContact | None,
    contact_name: str,
    contact_phone: str,
    contact_kind: str,
) -> DispatchEvent:
    """Ring one responder and open its confirmation window."""
    lang = session.language or "en"
    ev = _add_event(
        db, case,
        track=track,
        action=DispatchAction.call_placed.value,
        attempt=attempt,
        contact=contact,
        contact_name=contact_name,
        contact_phone=contact_phone,
        contact_kind=contact_kind,
        detail=f"outbound alert placed via {settings.TELEPHONY_PROVIDER} provider",
        due_in_sec=_window_sec(session),
    )
    db.flush()  # need ev.id for the provider payload

    message = (
        f"JeevanRekha emergency dispatch request. Reference {session.ref_code}. "
        f"Triage: EMERGENCY. Location area: "
        f"{session.region.label if session.region else 'unknown'}. "
        f"Press 1 to confirm you are responding."
    )
    result = get_provider().place_dispatch_alert(
        to_phone=contact_phone,
        to_name=contact_name,
        kind=contact_kind,
        message=message,
        call_ref=session.ref_code,
        case_id=case.id,
        track=track,
        event_id=ev.id,
    )
    if not result.ok:
        # Transport failure must never be silent: resolve this attempt and
        # let the escalation path move to the next contact immediately.
        ev.resolved = True
        ev.action = DispatchAction.failed.value
        ev.detail = f"provider failure: {result.detail}"
        _add_event(db, case, track=track, action=DispatchAction.failed.value,
                   attempt=attempt, contact=contact, detail=result.detail)
        db.flush()
        _escalate_track(db, case, session, track, failed_event=ev)
        return ev

    # SMS supplement (real providers deliver; simulated logs).
    get_provider().send_sms(
        contact_phone,
        f"JeevanRekha EMERGENCY ref {session.ref_code}: maternal/newborn danger sign. "
        f"Area: {session.region.label if session.region else 'unknown'}. Please respond.",
    )
    return ev


# --------------------------------------------------------------------------
# Case lifecycle
# --------------------------------------------------------------------------

def start_dispatch(db: Session, session: CallSession) -> DispatchCase:
    """Open a dispatch case and fire BOTH tracks at once."""
    case = DispatchCase(call_id=session.id, status=DispatchStatus.active.value)
    db.add(case)
    db.flush()

    region = session.region
    window = _window_sec(session)

    # --- Track A: official emergency ambulance -----------------------------
    amb_number = region.emergency_number if region else "108"
    amb_name = f"State emergency ambulance ({amb_number})"
    _place_alert(
        db, case, session,
        track=DispatchTrack.ambulance.value,
        attempt=1,
        contact=None,
        contact_name=amb_name,
        contact_phone=amb_number,
        contact_kind=ContactKind.ambulance_node.value,
    )
    _say(db, session, "st_amb_requested", number=amb_number)

    # --- Track B: local backup chain ----------------------------------------
    chain = _backup_chain(db, region.id if region else None)
    if chain:
        first = chain[0]
        _place_alert(
            db, case, session,
            track=DispatchTrack.backup.value,
            attempt=1,
            contact=first,
            contact_name=first.name,
            contact_phone=first.phone,
            contact_kind=first.kind,
        )
        _say(db, session, "st_backup_requested",
             name=first.name, kind=i18n.t(session.language, f"kind_{first.kind}"))
    else:
        # No configured backup for this area is itself an incident: raise an
        # operator alert immediately rather than pretending track B exists.
        _add_event(db, case, track=DispatchTrack.operator.value,
                   action=DispatchAction.operator_alert.value,
                   detail="no backup contacts configured for region")
        _say(db, session, "st_operator_alerted")
        db.add(IncidentLog(call_id=session.id, kind="no_backup_contacts",
                           detail=f"region_id={region.id if region else None}"))

    db.flush()
    log.info("dispatch case %s opened for call %s (window=%ss, backup chain=%d)",
             case.id, session.ref_code, window, len(chain))
    return case


def _backup_chain(db: Session, region_id: int | None) -> list[BackupContact]:
    if region_id is None:
        return []
    rows = db.scalars(
        select(BackupContact)
        .where(
            BackupContact.region_id == region_id,
            BackupContact.is_active.is_(True),
            BackupContact.kind.in_([
                ContactKind.asha.value,
                ContactKind.phc.value,
                ContactKind.local_transport.value,
            ]),
        )
        .order_by(BackupContact.priority, BackupContact.id)
    ).all()
    return list(rows)


def _district_control(db: Session, region_id: int | None) -> BackupContact | None:
    if region_id is None:
        return None
    return db.scalars(
        select(BackupContact).where(
            BackupContact.region_id == region_id,
            BackupContact.is_active.is_(True),
            BackupContact.kind == ContactKind.district_control.value,
        ).order_by(BackupContact.priority)
    ).first()


def pending_event(db: Session, case_id: int, track: str) -> DispatchEvent | None:
    return db.scalars(
        select(DispatchEvent).where(
            DispatchEvent.case_id == case_id,
            DispatchEvent.track == track,
            DispatchEvent.action == DispatchAction.call_placed.value,
            DispatchEvent.resolved.is_(False),
        )
    ).first()


def track_confirmed(db: Session, case_id: int, track: str) -> bool:
    ev = db.scalars(
        select(DispatchEvent).where(
            DispatchEvent.case_id == case_id,
            DispatchEvent.track == track,
            DispatchEvent.action == DispatchAction.confirmed.value,
        )
    ).first()
    return ev is not None


def _stand_down_other_track(db: Session, case: DispatchCase, session: CallSession, confirmed_track: str) -> None:
    other = (
        DispatchTrack.backup.value
        if confirmed_track == DispatchTrack.ambulance.value
        else DispatchTrack.ambulance.value
    )
    ev = pending_event(db, case.id, other)
    if ev:
        ev.resolved = True
        ev.detail = (ev.detail or "") + f" | stood down after confirmation on {confirmed_track} track"
        _add_event(db, case, track=other, action=DispatchAction.escalated.value,
                   attempt=ev.attempt, contact_id=ev.contact_id,
                   contact_name=ev.contact_name, contact_phone=ev.contact_phone,
                   contact_kind=ev.contact_kind,
                   detail=f"stood down: confirmation received on {confirmed_track} track")


def confirm(db: Session, case: DispatchCase, track: str, *, by: str, contact_id: int | None = None) -> None:
    """Human confirmation that help is actually moving."""
    session = db.get(CallSession, case.call_id)
    ev = pending_event(db, case.id, track)
    attempt = ev.attempt if ev else 1
    name = ev.contact_name if ev else ""
    phone = ev.contact_phone if ev else ""
    kind = ev.contact_kind if ev else ""
    if ev:
        ev.resolved = True
        ev.detail = (ev.detail or "") + f" | confirmed by {by}"

    _add_event(db, case, track=track, action=DispatchAction.confirmed.value,
               attempt=attempt, contact_id=contact_id or (ev.contact_id if ev else None),
               contact_name=name, contact_phone=phone, contact_kind=kind,
               detail=f"confirmation received via {by}")

    now = utcnow()
    if track == DispatchTrack.ambulance.value:
        case.ambulance_confirmed_at = now
        if session:
            _say(db, session, "st_amb_confirmed", time=ist_str(now))
    else:
        case.backup_confirmed_at = now
        if session:
            _say(db, session, "st_backup_confirmed", name=name)

    if case.confirmed_at is None:
        case.confirmed_at = now
        case.status = DispatchStatus.confirmed.value
        _stand_down_other_track(db, case, session, track)
    if case.ambulance_confirmed_at and case.backup_confirmed_at and session:
        _say(db, session, "st_both_confirmed")
    db.flush()


def decline(db: Session, case: DispatchCase, track: str, *, by: str) -> None:
    """A responder explicitly cannot help: escalate immediately, no retry."""
    session = db.get(CallSession, case.call_id)
    ev = pending_event(db, case.id, track)
    if ev:
        ev.resolved = True
        ev.detail = (ev.detail or "") + f" | declined by {by}"
        _add_event(db, case, track=track, action=DispatchAction.declined.value,
                   attempt=ev.attempt, contact_id=ev.contact_id,
                   contact_name=ev.contact_name, contact_phone=ev.contact_phone,
                   contact_kind=ev.contact_kind, detail=f"declined via {by}")
        _escalate_track(db, case, session, track, from_event=ev, skip_retry=True)
    db.flush()


def cancel(db: Session, case: DispatchCase, reason: str) -> None:
    session = db.get(CallSession, case.call_id)
    for track in (DispatchTrack.ambulance.value, DispatchTrack.backup.value):
        ev = pending_event(db, case.id, track)
        if ev:
            ev.resolved = True
    _add_event(db, case, track=DispatchTrack.operator.value,
               action=DispatchAction.escalated.value, detail=f"cancelled: {reason}")
    case.status = DispatchStatus.cancelled.value
    if session:
        _say(db, session, "st_cancelled")
    db.flush()


# --------------------------------------------------------------------------
# Timeout processing (driven by scheduler.tick)
# --------------------------------------------------------------------------

def process_timeouts(db: Session) -> int:
    """Resolve every attempt whose confirmation window has closed.

    Returns the number of timeouts processed. Never raises out of a single
    case: one broken case must not stop the others from escalating.
    """
    now = utcnow()
    due = db.scalars(
        select(DispatchEvent).where(
            DispatchEvent.action == DispatchAction.call_placed.value,
            DispatchEvent.resolved.is_(False),
            DispatchEvent.due_at.is_not(None),
            DispatchEvent.due_at <= now,
        )
    ).all()
    processed = 0
    for ev in due:
        try:
            case = db.get(DispatchCase, ev.case_id)
            if case is None or case.status in (
                DispatchStatus.confirmed.value,
                DispatchStatus.cancelled.value,
            ):
                ev.resolved = True
                continue
            session = db.get(CallSession, case.call_id)
            ev.resolved = True
            ev.detail = (ev.detail or "") + " | confirmation window closed"
            _add_event(db, case, track=ev.track, action=DispatchAction.timeout.value,
                       attempt=ev.attempt, contact_id=ev.contact_id,
                       contact_name=ev.contact_name, contact_phone=ev.contact_phone,
                       contact_kind=ev.contact_kind,
                       detail=f"no confirmation within window ({_window_sec(session)}s)")
            _escalate_track(db, case, session, ev.track, from_event=ev)
            db.commit()
            processed += 1
        except Exception:  # noqa: BLE001 - escalation must survive anything
            db.rollback()
            log.exception("timeout processing failed for event %s", ev.id)
            db.add(IncidentLog(kind="timeout_processing_error", detail=f"event_id={ev.id}"))
            db.commit()
    return processed


def _escalate_track(
    db: Session,
    case: DispatchCase,
    session: CallSession | None,
    track: str,
    *,
    from_event: DispatchEvent | None = None,
    failed_event: DispatchEvent | None = None,
    skip_retry: bool = False,
) -> None:
    """Move one track forward: retry -> next contact -> operator alert."""
    ev = from_event or failed_event
    attempt = ev.attempt if ev else 1
    region_id = session.region_id if session else None

    if track == DispatchTrack.ambulance.value:
        max_attempts = settings.DISPATCH_MAX_ATTEMPTS_PER_CONTACT
        if not skip_retry and attempt < max_attempts:
            number = ev.contact_phone if ev else (
                session.region.emergency_number if session and session.region else "108"
            )
            name = ev.contact_name if ev else f"State emergency ambulance ({number})"
            _place_alert(db, case, session, track=track, attempt=attempt + 1,
                         contact=None, contact_name=name, contact_phone=number,
                         contact_kind=ContactKind.ambulance_node.value)
            if session:
                _say(db, session, "st_amb_timeout_retry", number=number, attempt=attempt + 1)
            db.flush()
            return
        # Retries spent: hand to district control room if configured.
        dc = _district_control(db, region_id)
        if dc and not track_confirmed(db, case.id, track):
            already = any(
                e.contact_id == dc.id for e in case.events if e.track == track
            )
            if not already:
                _place_alert(db, case, session, track=track, attempt=1,
                             contact=dc, contact_name=dc.name, contact_phone=dc.phone,
                             contact_kind=dc.kind)
                if session:
                    _say(db, session, "st_timeout",
                         name=ev.contact_name if ev else "ambulance service",
                         next_name=dc.name)
                db.flush()
                return
        _operator_alert(db, case, session, track,
                        detail="ambulance track exhausted (retries + district control)")
        db.flush()
        _maybe_mark_exhausted(db, case, session)
        return

    # --- backup track --------------------------------------------------------
    chain = _backup_chain(db, region_id)
    current_id = ev.contact_id if ev else None
    max_attempts = settings.DISPATCH_MAX_ATTEMPTS_PER_CONTACT

    if not skip_retry and attempt < max_attempts and current_id is not None:
        contact = db.get(BackupContact, current_id)
        if contact and contact.is_active:
            _place_alert(db, case, session, track=track, attempt=attempt + 1,
                         contact=contact, contact_name=contact.name,
                         contact_phone=contact.phone, contact_kind=contact.kind)
            db.flush()
            return

    # Move to the next link in the chain.
    idx = next((i for i, c in enumerate(chain) if c.id == current_id), -1)
    nxt = chain[idx + 1] if idx + 1 < len(chain) else None
    if nxt:
        _place_alert(db, case, session, track=track, attempt=1,
                     contact=nxt, contact_name=nxt.name,
                     contact_phone=nxt.phone, contact_kind=nxt.kind)
        if session:
            _say(db, session, "st_timeout",
                 name=ev.contact_name if ev else "previous contact",
                 next_name=nxt.name)
        db.flush()
        return

    _operator_alert(db, case, session, track, detail="backup chain exhausted")
    db.flush()
    _maybe_mark_exhausted(db, case, session)


def _operator_alert(db: Session, case: DispatchCase, session: CallSession | None, track: str, detail: str) -> None:
    _add_event(db, case, track=DispatchTrack.operator.value,
               action=DispatchAction.operator_alert.value,
               detail=f"{track} track: {detail}",
               resolved=False)  # stays in the follow-up queue until acknowledged
    if session:
        _say(db, session, "st_operator_alerted")
    db.add(IncidentLog(call_id=session.id if session else None,
                       kind="operator_alert", detail=f"case={case.id} {track}: {detail}"))


def _maybe_mark_exhausted(db: Session, case: DispatchCase, session: CallSession | None) -> None:
    """Case is exhausted when neither track can escalate further and nothing
    is confirmed."""
    if case.status != DispatchStatus.active.value:
        return
    amb_done = (
        track_confirmed(db, case.id, DispatchTrack.ambulance.value)
        or pending_event(db, case.id, DispatchTrack.ambulance.value) is None
    )
    back_done = (
        track_confirmed(db, case.id, DispatchTrack.backup.value)
        or pending_event(db, case.id, DispatchTrack.backup.value) is None
    )
    amb_exhausted = amb_done and not track_confirmed(db, case.id, DispatchTrack.ambulance.value)
    back_exhausted = back_done and not track_confirmed(db, case.id, DispatchTrack.backup.value)
    if amb_exhausted and back_exhausted:
        case.status = DispatchStatus.exhausted.value
        case.exhausted_at = utcnow()
        _add_event(db, case, track=DispatchTrack.operator.value,
                   action=DispatchAction.exhausted.value,
                   detail="all contacts on both tracks exhausted without confirmation",
                   resolved=False)
        if session:
            _say(db, session, "st_exhausted")
        db.add(IncidentLog(call_id=session.id if session else None,
                           kind="dispatch_exhausted", detail=f"case={case.id}"))
