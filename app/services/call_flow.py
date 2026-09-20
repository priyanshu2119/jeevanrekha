"""The fixed-sequence call flow (channel-agnostic state machine).

One implementation drives every channel: the simulated phone console (DTMF),
the low-bandwidth web companion (buttons mapped to the same digits), and --
in production -- Exotel/Twilio voice webhooks. Channels render ``Prompt``
lists (i18n keys); all decision logic lives here and in the pure rules
engine.

State is derived entirely from database rows, never from memory, so a
restart mid-call loses nothing.

Safety properties enforced here:
* Silence and invalid input repeat ONCE, then are recorded as UNCLEAR --
  which the rules engine treats as a positive red flag.
* A session that goes idle mid-triage is finalised by the watchdog with the
  answers collected so far (missing = unclear = escalate). No call ever
  evaporates without one of the three defined outcomes.
* The flow never branches on free text. Only digits 1/2/3 (or the exact
  button equivalents) are understood.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..engine import i18n, rules
from ..engine.questions import questions_for
from ..models import (
    Answer,
    AnswerValue,
    CallSession,
    CallStatus,
    Channel,
    IncidentLog,
    StatusMessage,
    Tier,
    TriageResult,
    Track,
    utcnow,
)
from . import dispatch as dispatch_service

log = logging.getLogger("jr.flow")

# Flow states (derived, never stored).
S_LANGUAGE = "language"
S_TRACK = "track"
S_QUESTIONS = "questions"
S_RESULT = "result"
S_ENDED = "ended"

DIGIT_YES = "1"
DIGIT_NO = "2"
DIGIT_UNCLEAR = "3"

MAX_LANGUAGE_TRIES = 3
MAX_TRACK_TRIES = 3


@dataclass
class Prompt:
    key: str
    params: dict = field(default_factory=dict)
    kind: str = "ivr"  # ivr | guidance -- lets channels style them differently


@dataclass
class FlowStep:
    """Result of feeding one input to the flow."""
    state: str
    prompts: list[Prompt]
    result_ready: bool = False
    tier: str | None = None
    ended: bool = False


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------

def generate_ref(db: Session) -> str:
    for _ in range(50):
        ref = "JR-" + "".join(secrets.choice("0123456789") for _ in range(5))
        exists = db.scalars(
            select(CallSession.id).where(CallSession.ref_code == ref)
        ).first()
        if not exists:
            return ref
    raise RuntimeError("could not allocate a unique call reference")


def start_call(
    db: Session,
    *,
    channel: Channel,
    region_id: int | None,
    caller_phone: str | None = None,
    created_by_user_id: int | None = None,
    language: str | None = None,
) -> CallSession:
    session = CallSession(
        ref_code=generate_ref(db),
        channel=channel.value,
        caller_phone=caller_phone,
        region_id=region_id,
        language=language,
        status=CallStatus.in_progress.value,
        created_by_user_id=created_by_user_id,
    )
    db.add(session)
    db.flush()
    log.info("call %s started (channel=%s region=%s)", session.ref_code, channel.value, region_id)
    return session


def get_state(db: Session, session: CallSession) -> str:
    if session.status != CallStatus.in_progress.value:
        return S_ENDED
    if not session.language:
        return S_LANGUAGE
    if not session.track:
        return S_TRACK
    total = len(questions_for(session.track))
    answered = len(session.answers)
    if answered < total:
        return S_QUESTIONS
    return S_RESULT


def current_question(session: CallSession):
    qs = questions_for(session.track)
    answered_ids = {a.question_id for a in session.answers}
    for q in qs:
        if q.id not in answered_ids:
            return q
    return None


def opening_prompts(db: Session, session: CallSession) -> list[Prompt]:
    """Prompts to render when a channel first connects (or refreshes)."""
    state = get_state(db, session)
    if state == S_LANGUAGE:
        return [Prompt("ivr_welcome"), Prompt("ivr_lang_prompt")]
    if state == S_TRACK:
        return [Prompt("ivr_track_prompt")]
    if state == S_QUESTIONS:
        q = current_question(session)
        total = len(questions_for(session.track))
        return _question_prompts(q, total)
    if state == S_RESULT:
        return _result_prompts(db, session)
    return []


def _question_prompts(q, total: int) -> list[Prompt]:
    prompts = [
        Prompt("ivr_q_prefix", {"n": q.order, "total": total}),
        Prompt(f"q_{q.id}"),
    ]
    hint_key = f"h_{q.id}"
    if hint_key in i18n.PACKS["en"]:
        prompts.append(Prompt(hint_key))
    prompts.append(Prompt("ivr_answer_prompt"))
    return prompts


def _result_prompts(db: Session, session: CallSession) -> list[Prompt]:
    result = session.result
    if result is None:  # should not happen; finalise defensively
        finalise(db, session)
        result = session.result
    prompts = [Prompt(f"ivr_result_{result.tier}")]
    for key in result.guidance:
        prompts.append(Prompt(key, kind="guidance"))
    prompts.append(Prompt("ivr_ref", {"ref": session.ref_code}))
    if result.tier != Tier.emergency.value:
        prompts.append(Prompt("ivr_goodbye"))
    return prompts


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------

def handle_digit(db: Session, session: CallSession, digit: str) -> FlowStep:
    digit = (digit or "").strip()
    state = get_state(db, session)

    if state == S_ENDED:
        return FlowStep(state=S_ENDED, prompts=[], ended=True)

    if state == S_LANGUAGE:
        lang = i18n.LANGUAGE_BY_DIGIT.get(digit)
        if lang:
            session.language = lang
            db.flush()
            return FlowStep(state=S_TRACK, prompts=[Prompt("ivr_track_prompt")])
        tries = _bump_meta(db, session, "lang_tries")
        if tries >= MAX_LANGUAGE_TRIES:
            # Never get stuck: fall back to Hindi (widest reach in target
            # regions) and keep moving. Language choice has no effect on
            # triage safety.
            session.language = "hi"
            db.add(IncidentLog(call_id=session.id, kind="language_defaulted",
                               detail="invalid digits at language selection; defaulted to hi"))
            db.flush()
            return FlowStep(state=S_TRACK, prompts=[Prompt("ivr_track_prompt")])
        return FlowStep(state=S_LANGUAGE, prompts=[Prompt("ivr_invalid"), Prompt("ivr_lang_prompt")])

    if state == S_TRACK:
        track = {DIGIT_YES: Track.maternal.value, DIGIT_NO: Track.newborn.value}.get(digit)
        if track:
            session.track = track
            db.flush()
            q = current_question(session)
            total = len(questions_for(track))
            return FlowStep(state=S_QUESTIONS, prompts=_question_prompts(q, total))
        tries = _bump_meta(db, session, "track_tries")
        if tries >= MAX_TRACK_TRIES:
            # Safety default: proceed on the maternal track (missing answers
            # become UNCLEAR -> escalate). Logged for audit.
            session.track = Track.maternal.value
            db.add(IncidentLog(call_id=session.id, kind="track_defaulted",
                               detail="invalid digits at track selection; defaulted to maternal"))
            db.flush()
            q = current_question(session)
            total = len(questions_for(session.track))
            return FlowStep(state=S_QUESTIONS, prompts=_question_prompts(q, total))
        return FlowStep(state=S_TRACK, prompts=[Prompt("ivr_invalid"), Prompt("ivr_track_prompt")])

    if state == S_QUESTIONS:
        q = current_question(session)
        value = {
            DIGIT_YES: AnswerValue.yes.value,
            DIGIT_NO: AnswerValue.no.value,
            DIGIT_UNCLEAR: AnswerValue.unclear.value,
        }.get(digit)
        if value is None:
            tries = _bump_meta(db, session, f"invalid_{q.id}")
            if tries > settings.IVR_MAX_INVALID:
                return _record_answer(db, session, q, AnswerValue.unclear.value,
                                      ambiguous=True, repeats=tries)
            return FlowStep(state=S_QUESTIONS,
                            prompts=[Prompt("ivr_invalid"), Prompt(f"q_{q.id}"),
                                     Prompt("ivr_answer_prompt")])
        ambiguous = value == AnswerValue.unclear.value
        return _record_answer(db, session, q, value, ambiguous=ambiguous)

    if state == S_RESULT:
        # Caller pressed something during result/guidance playback. On the
        # emergency tier the call stays open (status updates keep flowing);
        # otherwise any key acknowledges and ends the call.
        if session.result and session.result.tier == Tier.emergency.value:
            return FlowStep(state=S_RESULT, prompts=[Prompt("st_waiting")],
                            result_ready=True, tier=Tier.emergency.value)
        end_call(db, session, CallStatus.completed)
        return FlowStep(state=S_ENDED, prompts=[Prompt("ivr_goodbye")], ended=True)

    return FlowStep(state=S_ENDED, prompts=[], ended=True)


def handle_silence(db: Session, session: CallSession) -> FlowStep:
    """No input within the channel's timeout. Repeat once, then UNCLEAR."""
    state = get_state(db, session)
    if state == S_QUESTIONS:
        q = current_question(session)
        tries = _bump_meta(db, session, f"silence_{q.id}")
        if tries > settings.IVR_NO_INPUT_REPEATS:
            return _record_answer(db, session, q, AnswerValue.unclear.value,
                                  ambiguous=True, repeats=tries)
        return FlowStep(state=S_QUESTIONS,
                        prompts=[Prompt("ivr_silence"), Prompt(f"q_{q.id}"),
                                 Prompt("ivr_answer_prompt")])
    if state == S_LANGUAGE:
        tries = _bump_meta(db, session, "lang_tries")
        if tries >= MAX_LANGUAGE_TRIES:
            session.language = "hi"
            db.add(IncidentLog(call_id=session.id, kind="language_defaulted",
                               detail="silence at language selection; defaulted to hi"))
            db.flush()
            return FlowStep(state=S_TRACK, prompts=[Prompt("ivr_track_prompt")])
        return FlowStep(state=S_LANGUAGE, prompts=[Prompt("ivr_silence"), Prompt("ivr_lang_prompt")])
    if state == S_TRACK:
        tries = _bump_meta(db, session, "track_tries")
        if tries >= MAX_TRACK_TRIES:
            session.track = Track.maternal.value
            db.add(IncidentLog(call_id=session.id, kind="track_defaulted",
                               detail="silence at track selection; defaulted to maternal"))
            db.flush()
            q = current_question(session)
            total = len(questions_for(session.track))
            return FlowStep(state=S_QUESTIONS, prompts=_question_prompts(q, total))
        return FlowStep(state=S_TRACK, prompts=[Prompt("ivr_silence"), Prompt("ivr_track_prompt")])
    return FlowStep(state=state, prompts=[])


def _record_answer(
    db: Session,
    session: CallSession,
    q,
    value: str,
    *,
    ambiguous: bool,
    repeats: int = 0,
) -> FlowStep:
    db.add(Answer(
        call_id=session.id,
        question_id=q.id,
        value=value,
        ambiguous=ambiguous,
        repeats=repeats,
        answered_at=utcnow(),
    ))
    db.flush()
    db.refresh(session)

    prompts: list[Prompt] = []
    if ambiguous:
        prompts.append(Prompt("ivr_unclear_ack"))

    nxt = current_question(session)
    if nxt is not None:
        total = len(questions_for(session.track))
        prompts += _question_prompts(nxt, total)
        return FlowStep(state=S_QUESTIONS, prompts=prompts)

    # Last answer recorded -> evaluate deterministically and finalise.
    finalise(db, session)
    prompts += _result_prompts(db, session)
    tier = session.result.tier if session.result else None
    if tier == Tier.emergency.value:
        # Call stays open: live status updates keep arriving until a human
        # confirms or the chain exhausts.
        return FlowStep(state=S_RESULT, prompts=prompts, result_ready=True, tier=tier)
    end_call(db, session, CallStatus.completed)
    return FlowStep(state=S_ENDED, prompts=prompts, result_ready=True, tier=tier, ended=True)


# --------------------------------------------------------------------------
# Finalisation (the "never silently fail" guarantee)
# --------------------------------------------------------------------------

def finalise(db: Session, session: CallSession) -> TriageResult:
    """Produce the one-and-only triage result for a session.

    Deterministic path: evaluate the recorded answers. If anything at all
    goes wrong, the fail-safe path assigns the EMERGENCY tier and logs an
    incident -- a caller is never left without an outcome.
    """
    if session.result is not None:
        return session.result

    answers = {a.question_id: a.value for a in session.answers}
    track = session.track or Track.maternal.value
    try:
        decision = rules.evaluate(track, answers)
        fail_safe = False
    except Exception:  # noqa: BLE001 - the fail-safe IS the point
        log.exception("triage engine failed for call %s; applying fail-safe", session.ref_code)
        decision = rules.evaluate_fail_safe(track)
        fail_safe = True
        db.add(IncidentLog(call_id=session.id, kind="engine_fail_safe",
                           detail=f"rule={decision.rule}"))

    result = TriageResult(
        tier=decision.tier,
        flagged=list(decision.flagged),
        unclear_count=decision.unclear_count,
        guidance=list(decision.guidance),
        engine_version=settings.ENGINE_VERSION,
        fail_safe=fail_safe,
    )
    # Assign through the relationship so both sides stay in sync in this
    # session -- a stale None here would double-insert and violate the
    # one-result-per-call guarantee.
    session.result = result
    db.add(result)
    db.add(StatusMessage(call_id=session.id, kind="system",
                         message_key="triage_decided",
                         params={"tier": decision.tier, "rule": decision.rule}))
    db.flush()

    if decision.tier == Tier.emergency.value:
        dispatch_service.start_dispatch(db, session)
    db.flush()
    return result


def end_call(db: Session, session: CallSession, status: CallStatus) -> None:
    if session.status == CallStatus.in_progress.value:
        session.status = status.value
        session.ended_at = utcnow()
        db.flush()


def hangup(db: Session, session: CallSession) -> None:
    """Caller disconnected. If triage was underway (track chosen), finalise
    with the answers collected so far -- missing answers escalate. A caller
    who went silent mid-triage may be in trouble; we do not assume otherwise.
    """
    if session.status != CallStatus.in_progress.value:
        return
    if session.track and session.result is None:
        finalise(db, session)
        db.add(IncidentLog(call_id=session.id, kind="hangup_mid_triage",
                           detail=f"finalised with {len(session.answers)} answers"))
        if session.result and session.result.tier == Tier.emergency.value:
            # Dispatch is already running from finalise(); keep the case open
            # so responders still converge even though the line dropped.
            db.add(StatusMessage(call_id=session.id, kind="system",
                                 message_key="caller_disconnected_emergency_active",
                                 params={}))
    end_call(db, session, CallStatus.completed if session.result else CallStatus.abandoned)
    db.flush()


# --------------------------------------------------------------------------
# Watchdog (driven by scheduler tick)
# --------------------------------------------------------------------------

IDLE_FINALISE_AFTER = timedelta(minutes=10)


def finalise_idle_sessions(db: Session) -> int:
    cutoff = utcnow() - IDLE_FINALISE_AFTER
    idle = db.scalars(
        select(CallSession).where(
            CallSession.status == CallStatus.in_progress.value,
            CallSession.started_at <= cutoff,
        )
    ).all()
    count = 0
    for session in idle:
        if session.track:
            finalise(db, session)
            db.add(IncidentLog(call_id=session.id, kind="idle_finalised",
                               detail=f"{len(session.answers)} answers at watchdog finalise"))
            end_call(db, session, CallStatus.completed)
            count += 1
        else:
            # Never engaged with triage (hung up at language/track prompt).
            end_call(db, session, CallStatus.abandoned)
            db.add(IncidentLog(call_id=session.id, kind="idle_abandoned",
                               detail="no track selected"))
            count += 1
    db.flush()
    return count


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _bump_meta(db: Session, session: CallSession, key: str) -> int:
    """Per-question retry counters, stored as system status messages so they
    are part of the audit trail (no extra table, survives restarts)."""
    row = db.scalars(
        select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.kind == "meta",
            StatusMessage.message_key == key,
        )
    ).first()
    if row is None:
        row = StatusMessage(call_id=session.id, kind="meta", message_key=key,
                            params={"count": 1})
        db.add(row)
        db.flush()
        return 1
    count = int(row.params.get("count", 0)) + 1
    row.params = {**row.params, "count": count}
    db.flush()
    return count


def new_status_messages(db: Session, session: CallSession, after_id: int) -> list[StatusMessage]:
    return list(db.scalars(
        select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.id > after_id,
            StatusMessage.kind.in_(["status", "guidance", "system"]),
        ).order_by(StatusMessage.id)
    ).all())
