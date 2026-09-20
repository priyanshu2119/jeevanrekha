"""JSON serializers shared by the web companion, simulator and staff APIs."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine import i18n
from ..models import (
    CallSession,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    DispatchStatus,
    DispatchTrack,
    StatusMessage,
)
from ..services import call_flow
from ..services.dispatch import ist_str


def render_prompts(session: CallSession, prompts: list[call_flow.Prompt]) -> list[dict]:
    out = []
    for p in prompts:
        out.append({
            "key": p.key,
            "params": p.params,
            "kind": p.kind,
            "text": i18n.t(session.language, p.key, **p.params),
        })
    return out


def dispatch_summary(db: Session, session: CallSession) -> dict | None:
    case = db.scalars(
        select(DispatchCase).where(DispatchCase.call_id == session.id)
    ).first()
    if case is None:
        return None

    def track_state(track: str) -> dict:
        confirmed = db.scalars(
            select(DispatchEvent).where(
                DispatchEvent.case_id == case.id,
                DispatchEvent.track == track,
                DispatchEvent.action == DispatchAction.confirmed.value,
            )
        ).first()
        pending = db.scalars(
            select(DispatchEvent).where(
                DispatchEvent.case_id == case.id,
                DispatchEvent.track == track,
                DispatchEvent.action == DispatchAction.call_placed.value,
                DispatchEvent.resolved.is_(False),
            )
        ).first()
        attempts = len(db.scalars(
            select(DispatchEvent.id).where(
                DispatchEvent.case_id == case.id,
                DispatchEvent.track == track,
                DispatchEvent.action == DispatchAction.call_placed.value,
            )
        ).all())
        stood_down = db.scalars(
            select(DispatchEvent).where(
                DispatchEvent.case_id == case.id,
                DispatchEvent.track == track,
                DispatchEvent.action == DispatchAction.escalated.value,
                DispatchEvent.detail.like("stood down%"),
            )
        ).first()
        if confirmed:
            state = "confirmed"
        elif pending:
            state = "waiting"
        elif stood_down:
            # The other track confirmed; this one was deliberately retired.
            # Showing "exhausted" here would be dishonest in the other
            # direction -- it implies failure where there was none.
            state = "stood_down"
        elif attempts:
            state = "exhausted"
        else:
            state = "none"
        return {
            "state": state,
            "attempts": attempts,
            "current_contact": (pending.contact_name if pending else None),
            "current_phone": (pending.contact_phone if pending else None),
            "confirmed_at": ist_str(confirmed.created_at) if confirmed else None,
        }

    operator_alerts = len(db.scalars(
        select(DispatchEvent.id).where(
            DispatchEvent.case_id == case.id,
            DispatchEvent.track == DispatchTrack.operator.value,
            DispatchEvent.action == DispatchAction.operator_alert.value,
        )
    ).all())

    return {
        "case_id": case.id,
        "status": case.status,
        "ambulance": track_state(DispatchTrack.ambulance.value),
        "backup": track_state(DispatchTrack.backup.value),
        "operator_alerts": operator_alerts,
        "started_at": ist_str(case.started_at),
        "confirmed_at": ist_str(case.confirmed_at),
    }


def status_messages(db: Session, session: CallSession, after_id: int = 0) -> list[dict]:
    # Caller-facing feed only: "system" rows (triage_decided, watchdog notes)
    # stay in the database for audit and staff views, never shown to callers.
    rows = db.scalars(
        select(StatusMessage).where(
            StatusMessage.call_id == session.id,
            StatusMessage.id > after_id,
            StatusMessage.kind.in_(["status", "guidance"]),
        ).order_by(StatusMessage.id)
    ).all()
    out = []
    for m in rows:
        out.append({
            "id": m.id,
            "kind": m.kind,
            "key": m.message_key,
            "text": i18n.t(session.language, m.message_key, **m.params),
            "at": ist_str(m.created_at),
        })
    return out


def flow_snapshot(db: Session, session: CallSession, after_id: int = 0) -> dict:
    state = call_flow.get_state(db, session)
    prompts = call_flow.opening_prompts(db, session)
    snap: dict = {
        "ref": session.ref_code,
        "state": state,
        "language": session.language,
        "track": session.track,
        "prompts": render_prompts(session, prompts),
        "status_messages": status_messages(db, session, after_id),
        "ended": state == call_flow.S_ENDED,
    }
    if state == call_flow.S_QUESTIONS:
        q = call_flow.current_question(session)
        from ..engine.questions import questions_for
        snap["question"] = {
            "id": q.id,
            "order": q.order,
            "total": len(questions_for(session.track)),
            "severity": q.severity,
        }
    if session.result:
        snap["result"] = {
            "tier": session.result.tier,
            "flagged": session.result.flagged,
            "unclear_count": session.result.unclear_count,
            "guidance": session.result.guidance,
            "engine_version": session.result.engine_version,
            "fail_safe": session.result.fail_safe,
        }
        snap["dispatch"] = dispatch_summary(db, session)
    return snap


def get_session_by_ref(db: Session, ref: str) -> CallSession | None:
    return db.scalars(
        select(CallSession).where(CallSession.ref_code == ref)
    ).first()
