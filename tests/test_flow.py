"""End-to-end call-flow behaviour over the real state machine + database.

Covers the definition-of-done items that are about the CALLER's experience:
emergency triggers parallel routing with honest status, ambiguity escalates,
low urgency reassures without dismissal, and no call ever evaporates.
"""
from sqlalchemy import select

from app.engine.questions import questions_for
from app.models import (
    Answer,
    CallStatus,
    Channel,
    DispatchCase,
    DispatchEvent,
    DispatchTrack,
    StatusMessage,
    Tier,
)
from app.services import call_flow


def _walk_to_questions(db, region, lang_digit="1", track_digit="1"):
    session = call_flow.start_call(db, channel=Channel.phone_sim, region_id=region.id,
                                   caller_phone="9876500000")
    db.commit()
    call_flow.handle_digit(db, session, lang_digit)   # language
    db.commit()
    call_flow.handle_digit(db, session, track_digit)  # track
    db.commit()
    return session


def _answer_all(db, session, values: dict[str, str]):
    """Feed the fixed sequence; every question gets its scripted value."""
    track = session.track
    for q in questions_for(track):
        digit = {"yes": "1", "no": "2", "unclear": "3"}[values.get(q.id, "no")]
        call_flow.handle_digit(db, session, digit)
        db.commit()
    db.refresh(session)
    return session


class TestEmergencyFlow:
    def test_clear_emergency_triggers_both_tracks_and_honest_status(self, db, region):
        session = _walk_to_questions(db, region)
        _answer_all(db, session, {"m_bleeding": "yes"})

        assert session.result is not None
        assert session.result.tier == Tier.emergency.value

        case = db.scalars(select(DispatchCase).where(DispatchCase.call_id == session.id)).first()
        assert case is not None, "emergency must open a dispatch case"

        # BOTH tracks fired -- parallel, not sequential.
        amb = db.scalars(select(DispatchEvent).where(
            DispatchEvent.case_id == case.id,
            DispatchEvent.track == DispatchTrack.ambulance.value,
        )).all()
        backup = db.scalars(select(DispatchEvent).where(
            DispatchEvent.case_id == case.id,
            DispatchEvent.track == DispatchTrack.backup.value,
        )).all()
        assert amb, "track A (ambulance) must be contacted"
        assert backup, "track B (local backup) must be contacted in parallel"

        # Honest status wording: requested, NOT confirmed.
        msgs = db.scalars(select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.kind == "status",
        )).all()
        keys = [m.message_key for m in msgs]
        assert "st_amb_requested" in keys
        assert "st_backup_requested" in keys
        assert "st_amb_confirmed" not in keys, "must never claim confirmation it doesn't have"

        # Session stays open while routing runs.
        assert session.status == CallStatus.in_progress.value

    def test_newborn_emergency_also_routes(self, db, region):
        session = _walk_to_questions(db, region, track_digit="2")
        _answer_all(db, session, {"n_feeding": "yes", "n_sleepy": "yes"})
        assert session.result.tier == Tier.emergency.value
        assert session.result.flagged == ["not_feeding", "lethargy"] or \
               set(session.result.flagged) == {"not_feeding", "lethargy"}
        assert db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first() is not None


class TestAmbiguityEscalates:
    """Definition of done: 'a simulated caller giving an ambiguous or unclear
    answer is escalated, not passed through as safe'."""

    def test_explicit_dont_know_on_red_sign_is_emergency(self, db, region):
        session = _walk_to_questions(db, region)
        _answer_all(db, session, {"m_fits": "unclear"})
        assert session.result.tier == Tier.emergency.value
        amb = db.scalars(select(Answer).where(
            Answer.call_id == session.id, Answer.question_id == "m_fits")).first()
        assert amb.value == "unclear" and amb.ambiguous is True

    def test_silence_becomes_unclear_and_escalates(self, db, region):
        session = _walk_to_questions(db, region)
        # First question: stay silent through the repeat -> recorded UNCLEAR.
        step = call_flow.handle_silence(db, session)
        db.commit()
        assert step.state == call_flow.S_QUESTIONS  # repeated once
        step = call_flow.handle_silence(db, session)
        db.commit()
        first_q = questions_for(session.track)[0]
        ans = db.scalars(select(Answer).where(
            Answer.call_id == session.id, Answer.question_id == first_q.id)).first()
        assert ans is not None and ans.value == "unclear" and ans.ambiguous
        # m_bleeding is red -> the whole call is now an emergency.
        _answer_all(db, session, {})
        db.refresh(session)
        assert session.result.tier == Tier.emergency.value

    def test_invalid_input_becomes_unclear(self, db, region):
        session = _walk_to_questions(db, region)
        call_flow.handle_digit(db, session, "9")  # invalid
        db.commit()
        call_flow.handle_digit(db, session, "7")  # invalid again -> unclear
        db.commit()
        first_q = questions_for(session.track)[0]
        ans = db.scalars(select(Answer).where(
            Answer.call_id == session.id, Answer.question_id == first_q.id)).first()
        assert ans is not None and ans.value == "unclear"

    def test_hangup_mid_triage_finalises_as_emergency(self, db, region):
        """A caller who goes quiet mid-triage is never assumed to be fine."""
        session = _walk_to_questions(db, region)
        call_flow.handle_digit(db, session, "2")  # one clear "no"
        db.commit()
        call_flow.hangup(db, session)
        db.commit()
        db.refresh(session)
        assert session.result is not None
        # 9 of 10 maternal questions unanswered -> unclear -> red -> emergency
        assert session.result.tier == Tier.emergency.value
        assert session.status == CallStatus.completed.value


class TestLowUrgencyFlow:
    def test_all_no_is_reassurance_with_real_guidance(self, db, region):
        session = _walk_to_questions(db, region)
        _answer_all(db, session, {})
        assert session.result.tier == Tier.reassurance.value
        assert session.result.unclear_count == 0
        # Not dismissive: substantive guidance keys present.
        assert "r_not_dismissive" in session.result.guidance
        assert len(session.result.guidance) >= 5
        # No dispatch machinery for a fine caller.
        assert db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first() is None
        # Call completes cleanly.
        assert session.status == CallStatus.completed.value

    def test_amber_only_is_urgent_not_emergency(self, db, region):
        session = _walk_to_questions(db, region)
        _answer_all(db, session, {"m_swelling": "yes"})
        assert session.result.tier == Tier.urgent.value
        assert db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first() is None


class TestFlowRobustness:
    def test_language_selection_rejects_garbage_then_defaults(self, db, region):
        session = call_flow.start_call(db, channel=Channel.phone_sim, region_id=region.id)
        db.commit()
        for _ in range(3):
            call_flow.handle_digit(db, session, "9")
            db.commit()
        db.refresh(session)
        assert session.language == "hi"  # defaulted, never stuck

    def test_every_session_gets_exactly_one_result(self, db, region):
        session = _walk_to_questions(db, region)
        _answer_all(db, session, {"m_fever": "yes"})
        # Finalising again must be idempotent.
        r1 = call_flow.finalise(db, session)
        r2 = call_flow.finalise(db, session)
        assert r1.id == r2.id

    def test_multilingual_flow_hindi_and_tamil(self, db, region):
        """Core flow works in more than one language (DoD item)."""
        for lang_digit, expected in (("1", "hi"), ("5", "ta")):
            session = _walk_to_questions(db, region, lang_digit=lang_digit)
            assert session.language == expected
            _answer_all(db, session, {})
            assert session.result is not None
