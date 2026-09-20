"""Dispatch engine: parallel routing, honest status, auto-escalation.

Definition of done: 'If dispatch confirmation doesn't arrive within the
defined window, the backup contact is actively re-contacted, and this is
visible in the call log.' Every test here drives the real service functions
against the real database -- timeouts are forced by moving due_at into the
past, exactly as the scheduler would encounter them.
"""
from datetime import timedelta

from sqlalchemy import select

from app.models import (
    CallSession,
    Channel,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    DispatchStatus,
    DispatchTrack,
    IncidentLog,
    StatusMessage,
    Tier,
    utcnow,
)
from app.services import call_flow, dispatch
from app.engine.questions import questions_for


def _emergency_session(db, region) -> CallSession:
    session = call_flow.start_call(db, channel=Channel.phone_sim,
                                   region_id=region.id, caller_phone="9876500000")
    db.commit()
    call_flow.handle_digit(db, session, "2")  # english (wording assertions below)
    db.commit()
    call_flow.handle_digit(db, session, "1")  # maternal
    db.commit()
    for q in questions_for("maternal"):
        call_flow.handle_digit(db, session, "1" if q.id == "m_bleeding" else "2")
        db.commit()
    db.refresh(session)
    assert session.result.tier == Tier.emergency.value
    return session


def _force_timeouts(db):
    past = utcnow() - timedelta(seconds=1)
    pending = db.scalars(select(DispatchEvent).where(
        DispatchEvent.action == DispatchAction.call_placed.value,
        DispatchEvent.resolved.is_(False),
    )).all()
    for ev in pending:
        ev.due_at = past
    db.commit()
    return dispatch.process_timeouts(db)


def _events(db, case_id, **filters):
    q = select(DispatchEvent).where(DispatchEvent.case_id == case_id)
    for field, value in filters.items():
        q = q.where(getattr(DispatchEvent, field) == value)
    return list(db.scalars(q.order_by(DispatchEvent.id)).all())


class TestParallelStart:
    def test_both_tracks_start_together(self, db, region):
        session = _emergency_session(db, region)
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()
        assert case.status == DispatchStatus.active.value

        placed = _events(db, case.id, action=DispatchAction.call_placed.value)
        tracks = {ev.track for ev in placed}
        assert tracks == {DispatchTrack.ambulance.value, DispatchTrack.backup.value}

        # First backup contact is the highest-priority one (ASHA One).
        backup_ev = next(ev for ev in placed if ev.track == DispatchTrack.backup.value)
        assert backup_ev.contact_name == "ASHA One"

        # Both attempts have an open confirmation window.
        for ev in placed:
            assert ev.resolved is False
            assert ev.due_at is not None and ev.due_at > utcnow() - timedelta(seconds=5)

    def test_caller_status_is_honest_not_reassuring(self, db, region):
        session = _emergency_session(db, region)
        msgs = db.scalars(select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.kind == "status")).all()
        amb = next(m for m in msgs if m.message_key == "st_amb_requested")
        from app.engine import i18n
        text = i18n.t(session.language, amb.message_key, **amb.params)
        assert "NOT" in text or "not" in text  # explicitly says confirmation is pending
        assert "confirm" in text.lower()


class TestEscalation:
    def test_backup_timeout_recontacts_next_in_chain(self, db, region):
        """DoD: timeout -> the NEXT backup contact is actively contacted,
        visible in the log."""
        session = _emergency_session(db, region)
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()

        n = _force_timeouts(db)
        assert n >= 2  # both tracks timed out

        backup_events = _events(db, case.id, track=DispatchTrack.backup.value)
        actions = [(ev.action, ev.contact_name, ev.attempt) for ev in backup_events]
        # ASHA One attempt 1 placed -> timeout -> attempt 2 (retry) OR next contact.
        assert any(a[0] == DispatchAction.timeout.value for a in actions)
        # After enough timeouts the chain must have moved to PHC Two.
        _force_timeouts(db)
        backup_events = _events(db, case.id, track=DispatchTrack.backup.value)
        contacted = {ev.contact_name for ev in backup_events
                     if ev.action == DispatchAction.call_placed.value}
        assert "PHC Two" in contacted, f"chain did not advance: {contacted}"

        # And the caller was told, honestly.
        msgs = db.scalars(select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.message_key == "st_timeout")).all()
        assert msgs, "caller must be told about the escalation"

    def test_ambulance_timeout_retries_then_district_control(self, db, region):
        session = _emergency_session(db, region)
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()

        _force_timeouts(db)  # attempt 1 times out -> retry placed
        amb = _events(db, case.id, track=DispatchTrack.ambulance.value)
        assert any(ev.action == DispatchAction.call_placed.value and ev.attempt == 2
                   for ev in amb)

        _force_timeouts(db)  # attempt 2 times out -> district control room
        amb = _events(db, case.id, track=DispatchTrack.ambulance.value)
        dc = [ev for ev in amb if ev.contact_name == "District Control"
              and ev.action == DispatchAction.call_placed.value]
        assert dc, "ambulance track must escalate to district control"

    def test_full_chain_exhaustion_raises_operator_and_never_goes_silent(self, db, region):
        session = _emergency_session(db, region)
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()

        for _ in range(12):  # walk every retry/contact/window
            _force_timeouts(db)
            db.refresh(case)
            if case.status == DispatchStatus.exhausted.value:
                break

        db.refresh(case)
        assert case.status == DispatchStatus.exhausted.value
        assert case.exhausted_at is not None

        ops = _events(db, case.id, track=DispatchTrack.operator.value)
        assert any(ev.action == DispatchAction.operator_alert.value for ev in ops)

        # Caller heard the exhaustion honestly.
        msgs = db.scalars(select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.message_key == "st_exhausted")).all()
        assert msgs

        # Incident logged for audit.
        incidents = db.scalars(select(IncidentLog).where(
            IncidentLog.call_id == session.id,
            IncidentLog.kind == "dispatch_exhausted")).all()
        assert incidents

    def test_no_pending_events_remain_after_exhaustion(self, db, region):
        session = _emergency_session(db, region)
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()
        for _ in range(12):
            _force_timeouts(db)
            db.refresh(case)
            if case.status == DispatchStatus.exhausted.value:
                break
        pending = _events(db, case.id, action=DispatchAction.call_placed.value,
                          resolved=False)
        assert pending == []


class TestConfirmation:
    def test_confirm_marks_case_and_tells_caller(self, db, region):
        session = _emergency_session(db, region)
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()

        dispatch.confirm(db, case, DispatchTrack.ambulance.value, by="test-desk")
        db.commit()
        db.refresh(case)

        assert case.status == DispatchStatus.confirmed.value
        assert case.ambulance_confirmed_at is not None
        assert case.confirmed_at is not None

        msgs = db.scalars(select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.message_key == "st_amb_confirmed")).all()
        assert msgs

    def test_first_confirmation_stands_down_other_track(self, db, region):
        session = _emergency_session(db, region)
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()
        dispatch.confirm(db, case, DispatchTrack.ambulance.value, by="test-desk")
        db.commit()

        pending_backup = _events(db, case.id, track=DispatchTrack.backup.value,
                                 action=DispatchAction.call_placed.value, resolved=False)
        assert pending_backup == [], "backup track must stand down after confirmation"
        standdown = _events(db, case.id, track=DispatchTrack.backup.value,
                            action=DispatchAction.escalated.value)
        assert standdown and "stood down" in (standdown[-1].detail or "")

    def test_decline_escalates_immediately_without_retry(self, db, region):
        session = _emergency_session(db, region)
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()
        dispatch.decline(db, case, DispatchTrack.backup.value, by="test-desk")
        db.commit()

        backup = _events(db, case.id, track=DispatchTrack.backup.value)
        assert any(ev.action == DispatchAction.declined.value for ev in backup)
        # Next contact (PHC Two) contacted straight away -- no retry of ASHA One.
        placed_names = [ev.contact_name for ev in backup
                        if ev.action == DispatchAction.call_placed.value]
        assert placed_names[-1] == "PHC Two"

    def test_timeout_after_confirmation_resolves_quietly(self, db, region):
        session = _emergency_session(db, region)
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()
        dispatch.confirm(db, case, DispatchTrack.backup.value, by="test-desk")
        db.commit()
        # Any leftover due events must not re-escalate a confirmed case.
        before = len(_events(db, case.id))
        _force_timeouts(db)
        after = len(_events(db, case.id))
        db.refresh(case)
        assert case.status == DispatchStatus.confirmed.value
        assert after - before <= 1  # at most bookkeeping, no new alerts


class TestAuditTrail:
    def test_every_step_is_an_event_row(self, db, region):
        """The call log must show the whole story: placed, timeout, escalated,
        confirmed -- in order, with contact snapshots."""
        session = _emergency_session(db, region)
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()
        _force_timeouts(db)
        dispatch.confirm(db, case, DispatchTrack.backup.value, by="test-desk")
        db.commit()

        events = _events(db, case.id)
        assert len(events) >= 5
        # Snapshot integrity: contacted events carry name+phone even though
        # configuration could change later.
        for ev in events:
            if ev.action == DispatchAction.call_placed.value:
                assert ev.contact_phone
                assert ev.contact_name
        # Chronological.
        stamps = [ev.id for ev in events]
        assert stamps == sorted(stamps)
