#!/usr/bin/env python3
"""Seed a JeevanRekha deployment.

Creates:
* three regions with realistic parallel-routing chains (track B),
* staff accounts (admin / two ASHA workers / operator),
* fourteen days of operational history. The history is *computed*: every
  triage result comes from the real rules engine over the recorded answers,
  and every dispatch timeline is built from real event sequences (confirmed,
  escalated-then-confirmed, exhausted). Dashboards therefore show honest
  aggregates from day one, and any new call you make is indistinguishable
  from the seeded ones.

Usage:
    python scripts/seed.py            # seed if empty
    python scripts/seed.py --fresh    # delete the database and reseed
"""
from __future__ import annotations

import random
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.engine import rules  # noqa: E402
from app.engine.questions import questions_for  # noqa: E402
from app.models import (  # noqa: E402
    Answer,
    AnswerValue,
    BackupContact,
    CallSession,
    CallStatus,
    Channel,
    ContactKind,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    DispatchStatus,
    DispatchTrack,
    IncidentLog,
    Region,
    Role,
    StatusMessage,
    Tier,
    TriageResult,
    Track,
    User,
    utcnow,
)
from app.security import hash_password  # noqa: E402
from app.services import call_flow  # noqa: E402

random.seed(20260920)  # reproducible seeds


# --------------------------------------------------------------------------
# Configuration data
# --------------------------------------------------------------------------

REGIONS = [
    {
        "state": "Madhya Pradesh", "district": "Sehore", "block": "Ashta",
        "emergency_number": "108", "confirm_window_sec": 480,
        "contacts": [
            ("Sunita Verma (ASHA, Ward 1–5)", ContactKind.asha, "9425011223", 10,
             "covers the east cluster; has a motorcycle"),
            ("PHC Ashta — duty nurse", ContactKind.phc, "07562212345", 20,
             "24×7 duty room; landline rings at the nurse station"),
            ("Kailash Transport (local emergency vehicle)", ContactKind.local_transport,
             "9893344556", 30, "Bolero owner; night availability uncertain"),
            ("District Control Room, Sehore", ContactKind.district_control,
             "07562255100", 90, "escalation point when 108 does not confirm"),
        ],
    },
    {
        "state": "Maharashtra", "district": "Dharashiv", "block": "Osmanabad Rural",
        "emergency_number": "108", "confirm_window_sec": 360,
        "contacts": [
            ("Kavita Das (ASHA)", ContactKind.asha, "9822033445", 10, None),
            ("PHC Osmanabad Rural", ContactKind.phc, "02472212345", 20, None),
            ("Shivneri Patient Transport", ContactKind.local_transport, "9764055667", 30,
             "paid service, usually responds within 20 min"),
            ("District Control Room, Dharashiv", ContactKind.district_control,
             "02472255100", 90, None),
        ],
    },
    {
        "state": "Jharkhand", "district": "Ranchi", "block": "Kanke",
        "emergency_number": "108", "confirm_window_sec": 480,
        "contacts": [
            ("Anima Toppo (ASHA)", ContactKind.asha, "9431066778", 10, None),
            ("CHC Kanke", ContactKind.phc, "06512212345", 20, None),
            ("District Control Room, Ranchi", ContactKind.district_control,
             "06512255100", 90, None),
        ],
    },
]

USERS = [
    ("admin", "District Program Officer", Role.admin, "admin123", None, "en"),
    ("sunita", "Sunita Verma (ASHA)", Role.asha, "asha123", 0, "hi"),
    ("kavita", "Kavita Das (ASHA)", Role.asha, "asha123", 1, "mr"),
    ("operator", "Control Room Operator", Role.operator, "op123", None, "en"),
]

CALLER_POOL = [
    "9876500011", "9876500011", "9876500022", "9876500033", "9876500033",
    "9876500033", "9876500044", "9876500055", "9876500066", "9876500077",
    None, None, None,
]

# Scenarios: (track, {question_id: value}, dispatch_outcome)
# dispatch_outcome: None | "confirmed_fast" | "escalated_confirmed" | "exhausted"
SCENARIOS = [
    ("maternal", {"m_bleeding": "yes"}, "confirmed_fast"),
    ("newborn", {"n_feeding": "yes", "n_sleepy": "yes"}, "escalated_confirmed"),
    ("maternal", {"m_fits": "unclear"}, "confirmed_fast"),
    ("maternal", {}, None),
    ("newborn", {}, None),
    ("maternal", {"m_swelling": "yes"}, None),
    ("newborn", {"n_cord": "yes"}, None),
    ("maternal", {"m_fever": "yes", "m_headache": "yes"}, "exhausted"),
    ("newborn", {"n_temp_cold": "yes"}, "escalated_confirmed"),
    ("maternal", {}, None),
    ("maternal", {"m_vomiting": "unclear"}, None),
    ("newborn", {"n_stools": "yes"}, None),
    ("maternal", {"m_leaking": "yes"}, "confirmed_fast"),
    ("maternal", {}, None),
    ("newborn", {"n_jaundice": "yes"}, "escalated_confirmed"),
    ("maternal", {"m_movement": "yes"}, "confirmed_fast"),
    ("maternal", {}, None),
    ("newborn", {}, None),
    ("maternal", {"m_breathing": "yes"}, "exhausted"),
    ("newborn", {"n_breathing": "unclear"}, "confirmed_fast"),
    ("maternal", {"m_pain": "yes"}, "escalated_confirmed"),
    ("maternal", {}, None),
    ("newborn", {"n_temp_hot": "yes"}, "confirmed_fast"),
    ("maternal", {}, None),
    ("newborn", {}, None),
    ("maternal", {"m_swelling": "yes", "m_headache": "unclear"}, "confirmed_fast"),
    ("newborn", {"n_feeding": "unclear"}, "escalated_confirmed"),
    ("maternal", {}, None),
    ("newborn", {"n_fits": "yes"}, "confirmed_fast"),
    ("maternal", {}, None),
]


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

def seed_regions_and_users(db) -> list[Region]:
    regions = []
    for spec in REGIONS:
        region = Region(
            state=spec["state"], district=spec["district"], block=spec["block"],
            emergency_number=spec["emergency_number"],
            confirm_window_sec=spec["confirm_window_sec"],
        )
        db.add(region)
        db.flush()
        for name, kind, phone, priority, notes in spec["contacts"]:
            db.add(BackupContact(
                region_id=region.id, name=name, kind=kind.value,
                phone=phone, priority=priority, notes=notes,
            ))
        regions.append(region)

    for username, full_name, role, password, region_idx, language in USERS:
        pw_hash, salt = hash_password(password)
        db.add(User(
            username=username, full_name=full_name, role=role.value,
            region_id=regions[region_idx].id if region_idx is not None else None,
            password_hash=pw_hash, salt=salt, language=language,
        ))
    db.flush()
    return regions


def seed_history(db, regions: list[Region]) -> None:
    now = utcnow()
    for i, (track, positives, outcome) in enumerate(SCENARIOS):
        days_ago = random.uniform(0.5, 14)
        started = now - timedelta(days=days_ago)
        region = regions[i % len(regions)]
        caller = random.choice(CALLER_POOL)
        language = random.choice(["hi", "en", "mr", "bn", "ta", "te"])

        session = CallSession(
            ref_code=call_flow.generate_ref(db),
            channel=random.choice([Channel.phone_sim.value, Channel.web.value]),
            caller_phone=caller,
            region_id=region.id,
            language=language,
            track=track,
            status=CallStatus.completed.value,
            started_at=started,
            ended_at=started + timedelta(minutes=random.uniform(3, 12)),
        )
        db.add(session)
        db.flush()

        # Answers: positives as specified, everything else a clear "no".
        answers: dict[str, str] = {}
        t = started + timedelta(seconds=40)
        for q in questions_for(track):
            value = positives.get(q.id, AnswerValue.no.value)
            answers[q.id] = value
            db.add(Answer(
                call_id=session.id, question_id=q.id, value=value,
                ambiguous=value == AnswerValue.unclear.value,
                asked_at=t, answered_at=t + timedelta(seconds=random.randint(3, 9)),
            ))
            t += timedelta(seconds=random.randint(5, 9))

        # Triage computed by the real engine.
        decision = rules.evaluate(track, answers)
        result = TriageResult(
            call_id=session.id,
            tier=decision.tier,
            flagged=list(decision.flagged),
            unclear_count=decision.unclear_count,
            guidance=list(decision.guidance),
            decided_at=t,
            engine_version=settings.ENGINE_VERSION,
        )
        db.add(result)
        db.flush()

        if decision.tier == Tier.emergency.value and outcome:
            _seed_dispatch(db, session, region, started=t, outcome=outcome)

    db.flush()


def _seed_dispatch(db, session: CallSession, region: Region, started, outcome: str) -> None:
    case = DispatchCase(call_id=session.id, status=DispatchStatus.active.value, started_at=started)
    db.add(case)
    db.flush()

    amb_number = region.emergency_number
    amb_name = f"State emergency ambulance ({amb_number})"
    chain = sorted(
        [c for c in region.contacts if c.kind in (
            ContactKind.asha.value, ContactKind.phc.value, ContactKind.local_transport.value)],
        key=lambda c: c.priority,
    )
    dc = next((c for c in region.contacts if c.kind == ContactKind.district_control.value), None)

    def ev(track, action, attempt=1, contact=None, name=None, phone=None, kind=None,
           detail=None, at=None, resolved=True):
        e = DispatchEvent(
            case_id=case.id, track=track, action=action, attempt=attempt,
            contact_id=contact.id if contact else None,
            contact_name=name or (contact.name if contact else None),
            contact_phone=phone or (contact.phone if contact else None),
            contact_kind=kind or (contact.kind if contact else None),
            detail=detail, created_at=at or started, resolved=resolved,
        )
        db.add(e)
        return e

    def say(key, at, **params):
        db.add(StatusMessage(call_id=session.id, kind="status", message_key=key,
                             params=params, created_at=at))

    t0 = started
    ev(DispatchTrack.ambulance.value, DispatchAction.call_placed.value, 1,
       name=amb_name, phone=amb_number, kind=ContactKind.ambulance_node.value, at=t0)
    say("st_amb_requested", t0, number=amb_number)
    if chain:
        ev(DispatchTrack.backup.value, DispatchAction.call_placed.value, 1,
           contact=chain[0], at=t0)
        say("st_backup_requested", t0 + timedelta(seconds=2),
            name=chain[0].name, kind=chain[0].kind)

    if outcome == "confirmed_fast":
        at = t0 + timedelta(minutes=random.uniform(3, 6))
        ev(DispatchTrack.ambulance.value, DispatchAction.confirmed.value, 1,
           name=amb_name, phone=amb_number, kind=ContactKind.ambulance_node.value,
           detail="confirmation received via responder DTMF", at=at)
        case.ambulance_confirmed_at = at
        case.confirmed_at = at
        case.status = DispatchStatus.confirmed.value
        say("st_amb_confirmed", at, time=at.strftime("%H:%M IST"))
        if chain:
            ev(DispatchTrack.backup.value, DispatchAction.escalated.value, 1,
               contact=chain[0], detail="stood down: confirmation received on ambulance track",
               at=at + timedelta(seconds=5))

    elif outcome == "escalated_confirmed":
        w = timedelta(seconds=region.confirm_window_sec or settings.DISPATCH_CONFIRM_WINDOW_SEC)
        t1 = t0 + w
        ev(DispatchTrack.ambulance.value, DispatchAction.timeout.value, 1,
           name=amb_name, phone=amb_number, kind=ContactKind.ambulance_node.value,
           detail="no confirmation within window", at=t1)
        ev(DispatchTrack.ambulance.value, DispatchAction.call_placed.value, 2,
           name=amb_name, phone=amb_number, kind=ContactKind.ambulance_node.value, at=t1)
        say("st_amb_timeout_retry", t1, number=amb_number, attempt=2)
        if len(chain) > 1:
            t2 = t1 + timedelta(seconds=30)
            ev(DispatchTrack.backup.value, DispatchAction.timeout.value, 1,
               contact=chain[0], detail="no confirmation within window", at=t2)
            ev(DispatchTrack.backup.value, DispatchAction.call_placed.value, 1,
               contact=chain[1], at=t2)
            say("st_timeout", t2, name=chain[0].name, next_name=chain[1].name)
            at = t2 + timedelta(minutes=random.uniform(2, 5))
            ev(DispatchTrack.backup.value, DispatchAction.confirmed.value, 1,
               contact=chain[1], detail="confirmation received via responder DTMF", at=at)
            case.backup_confirmed_at = at
            case.confirmed_at = at
            case.status = DispatchStatus.confirmed.value
            say("st_backup_confirmed", at, name=chain[1].name)
            ev(DispatchTrack.ambulance.value, DispatchAction.escalated.value, 2,
               name=amb_name, phone=amb_number, kind=ContactKind.ambulance_node.value,
               detail="stood down: confirmation received on backup track",
               at=at + timedelta(seconds=5))
        else:
            at = t1 + timedelta(minutes=random.uniform(2, 5))
            ev(DispatchTrack.ambulance.value, DispatchAction.confirmed.value, 2,
               name=amb_name, phone=amb_number, kind=ContactKind.ambulance_node.value,
               detail="confirmation received via responder DTMF", at=at)
            case.ambulance_confirmed_at = at
            case.confirmed_at = at
            case.status = DispatchStatus.confirmed.value
            say("st_amb_confirmed", at, time=at.strftime("%H:%M IST"))

    elif outcome == "exhausted":
        w = timedelta(seconds=region.confirm_window_sec or settings.DISPATCH_CONFIRM_WINDOW_SEC)
        t = t0
        for attempt in (1, 2):
            t += w
            ev(DispatchTrack.ambulance.value, DispatchAction.timeout.value, attempt,
               name=amb_name, phone=amb_number, kind=ContactKind.ambulance_node.value,
               detail="no confirmation within window", at=t)
            if attempt < 2:
                ev(DispatchTrack.ambulance.value, DispatchAction.call_placed.value, attempt + 1,
                   name=amb_name, phone=amb_number, kind=ContactKind.ambulance_node.value, at=t)
                say("st_amb_timeout_retry", t, number=amb_number, attempt=attempt + 1)
        if dc:
            t += timedelta(seconds=30)
            ev(DispatchTrack.ambulance.value, DispatchAction.call_placed.value, 1,
               contact=dc, at=t)
            say("st_timeout", t, name=amb_name, next_name=dc.name)
            t += w
            ev(DispatchTrack.ambulance.value, DispatchAction.timeout.value, 1,
               contact=dc, detail="no confirmation within window", at=t)
        for idx, contact in enumerate(chain):
            if idx == 0:
                continue  # first contact already placed at t0
            t += timedelta(seconds=30)
            ev(DispatchTrack.backup.value, DispatchAction.call_placed.value, 1,
               contact=contact, at=t)
            say("st_timeout", t, name=chain[idx - 1].name, next_name=contact.name)
            t += w
            ev(DispatchTrack.backup.value, DispatchAction.timeout.value, 1,
               contact=contact, detail="no confirmation within window", at=t)
        t += timedelta(seconds=10)
        ev(DispatchTrack.operator.value, DispatchAction.operator_alert.value,
           detail="both tracks exhausted without confirmation", at=t)
        ev(DispatchTrack.operator.value, DispatchAction.exhausted.value,
           detail="all contacts on both tracks exhausted without confirmation", at=t)
        say("st_operator_alerted", t)
        say("st_exhausted", t + timedelta(seconds=2))
        case.status = DispatchStatus.exhausted.value
        case.exhausted_at = t
        db.add(IncidentLog(call_id=session.id, kind="dispatch_exhausted",
                           detail=f"case={case.id}", created_at=t))

    db.flush()


def main() -> None:
    fresh = "--fresh" in sys.argv
    if fresh:
        db_path = Path(settings.DATABASE_URL.replace("sqlite:///", ""))
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        print(f"removed {db_path}")

    init_db()
    db = SessionLocal()
    try:
        existing = db.query(User).count()
        if existing and not fresh:
            print("database already seeded; use --fresh to reseed from scratch")
            return
        regions = seed_regions_and_users(db)
        seed_history(db, regions)
        db.commit()
        calls = db.query(CallSession).count()
        print(f"seeded {len(regions)} regions, {len(USERS)} users, {calls} historical calls")
        print("staff logins: admin/admin123 · sunita/asha123 · kavita/asha123 · operator/op123")
    finally:
        db.close()


if __name__ == "__main__":
    main()
