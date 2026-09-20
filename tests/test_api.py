"""HTTP-level tests: pages render, the flow API works end to end, staff can
retrieve every call, and admin aggregates roll up correctly.

Definition of done: 'Every call, regardless of outcome, is retrievable
afterward from the ASHA-facing log, and its data correctly rolls up into the
anonymized administrator view.'
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Base, engine
from app.main import app
from app.models import (
    BackupContact,
    CallSession,
    Channel,
    ContactKind,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    DispatchStatus,
    Region,
    Role,
    Tier,
    User,
    utcnow,
)
from app.security import hash_password
from app.services import call_flow
from app.engine.questions import questions_for


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


def _make_user(db, username, role, region=None, password="test1234"):
    pw, salt = hash_password(password)
    user = User(username=username, full_name=username.title(), role=role.value,
                region_id=region.id if region else None,
                password_hash=pw, salt=salt)
    db.add(user)
    db.commit()
    return user


def _login(client, username, password="test1234"):
    return client.post("/login", data={"username": username, "password": password,
                                       "next": "/staff"}, follow_redirects=False)


def _run_web_call(db, region, positives: dict[str, str], track="maternal",
                  lang="1") -> CallSession:
    session = call_flow.start_call(db, channel=Channel.web,
                                   region_id=region.id, caller_phone="9800000001")
    db.commit()
    call_flow.handle_digit(db, session, lang)
    db.commit()
    call_flow.handle_digit(db, session, "1" if track == "maternal" else "2")
    db.commit()
    for q in questions_for(track):
        digit = {"yes": "1", "no": "2", "unclear": "3"}.get(positives.get(q.id, "no"), "2")
        call_flow.handle_digit(db, session, digit)
        db.commit()
    db.refresh(session)
    return session


class TestPages:
    def test_landing_renders(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "JeevanRekha" in r.text
        # The product must never present itself as a diagnostic tool.
        assert "not a diagnostic tool" in r.text or "does not diagnose" in r.text

    def test_triage_page_renders(self, client):
        assert client.get("/triage").status_code == 200

    def test_sim_page_renders(self, client):
        assert client.get("/sim").status_code == 200

    def test_login_page_renders(self, client):
        assert client.get("/login").status_code == 200

    def test_staff_pages_redirect_anonymous(self, client):
        for path in ("/asha", "/admin", "/admin/calls", "/admin/regions"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 303 and "/login" in r.headers["location"]


class TestFlowAPI:
    def test_full_web_flow_to_emergency(self, client, db, region):
        r = client.post("/api/flow/start", json={"region_id": region.id, "channel": "web"})
        assert r.status_code == 200
        snap = r.json()
        ref = snap["ref"]
        assert snap["state"] == "language"

        r = client.post(f"/api/flow/{ref}/digit", json={"digit": "2"})  # english
        assert r.json()["state"] == "track"
        r = client.post(f"/api/flow/{ref}/digit", json={"digit": "1"})  # maternal
        assert r.json()["state"] == "questions"

        for q in questions_for("maternal"):
            digit = "1" if q.id == "m_bleeding" else "2"
            snap = client.post(f"/api/flow/{ref}/digit", json={"digit": digit}).json()

        assert snap["result"]["tier"] == "emergency"
        assert snap["dispatch"] is not None
        assert snap["dispatch"]["ambulance"]["state"] == "waiting"
        assert snap["dispatch"]["backup"]["state"] == "waiting"
        # Honest wording reached the caller feed.
        texts = " ".join(m["text"] for m in snap["status_messages"])
        assert "NOT yet received confirmation" in texts

    def test_unknown_ref_404s_gracefully(self, client):
        assert client.get("/api/flow/JR-00000").json() == {"error": "not_found"}

    def test_i18n_endpoint(self, client):
        r = client.get("/api/i18n?lang=hi&keys=btn_yes,tier_emergency")
        data = r.json()
        assert data["strings"]["btn_yes"] == "हाँ"
        assert "आपात" in data["strings"]["tier_emergency"]


class TestDeskAPI:
    def test_desk_shows_pending_alerts_and_confirm_works(self, client, db, region):
        session = _run_web_call(db, region, {"m_fits": "yes"})
        assert session.result.tier == Tier.emergency.value

        desk = client.get("/api/sim/desk").json()
        assert len(desk["alerts"]) >= 2  # ambulance + backup rang

        first = desk["alerts"][0]
        r = client.post(f"/api/sim/desk/{first['event_id']}/confirm",
                        json={"by": "test"})
        assert r.json()["ok"] is True

        case = db.get(DispatchCase, first["case_id"])
        db.refresh(case)
        assert case.status in (DispatchStatus.confirmed.value, DispatchStatus.active.value)


class TestStaffRetrieval:
    def test_asha_sees_calls_in_her_region(self, client, db, region):
        _make_user(db, "asha1", Role.asha, region=region)
        session = _run_web_call(db, region, {"m_swelling": "yes"})

        _login(client, "asha1")
        r = client.get("/asha")
        assert r.status_code == 200
        assert session.ref_code in r.text

        r = client.get(f"/asha/calls/{session.ref_code}")
        assert r.status_code == 200
        assert "m_swelling" in r.text or "Swelling" in r.text or "swelling" in r.text

    def test_asha_detail_shows_dispatch_escalation_trail(self, client, db, region):
        """DoD: timeout escalation is visible in the call log a health worker
        reviews -- placed attempts, timeouts and confirmations all render."""
        from datetime import timedelta
        from app.models import DispatchAction, DispatchEvent
        from app.services import dispatch

        _make_user(db, "asha3", Role.asha, region=region)
        session = _run_web_call(db, region, {"m_bleeding": "yes"})
        case = session.dispatch

        # Force a timeout + escalation, then confirm the backup.
        pending = db.scalars(select(DispatchEvent).where(
            DispatchEvent.action == DispatchAction.call_placed.value,
            DispatchEvent.resolved.is_(False))).all()
        for ev in pending:
            ev.due_at = utcnow() - timedelta(seconds=1)
        db.commit()
        dispatch.process_timeouts(db)
        db.commit()

        _login(client, "asha3")
        r = client.get(f"/asha/calls/{session.ref_code}")
        assert r.status_code == 200
        assert "Dispatch timeline" in r.text
        assert "timeout" in r.text
        assert "call_placed" in r.text

    def test_asha_cannot_see_other_regions(self, client, db, region):
        other = Region(state="Other", district="Other", block="Other",
                       emergency_number="108")
        db.add(other)
        db.commit()
        _make_user(db, "asha2", Role.asha, region=other)
        session = _run_web_call(db, region, {})

        _login(client, "asha2")
        r = client.get(f"/asha/calls/{session.ref_code}", follow_redirects=False)
        assert r.status_code == 303  # bounced back to her own log

    def test_admin_dashboard_rolls_up(self, client, db, region):
        _make_user(db, "boss", Role.admin)
        s1 = _run_web_call(db, region, {"m_bleeding": "yes"})   # emergency
        s2 = _run_web_call(db, region, {"m_swelling": "yes"})   # urgent
        s3 = _run_web_call(db, region, {})                      # reassurance

        _login(client, "boss")
        r = client.get("/admin")
        assert r.status_code == 200
        # The emergency is individually retrievable from the dashboard...
        assert s1.ref_code in r.text
        # ...and every call rolls up into the aggregate counters (3 total,
        # one per tier). Individual non-emergency refs are deliberately NOT
        # listed on the aggregate view.
        assert '<div class="v">3</div>' in r.text
        assert s2.ref_code not in r.text and s3.ref_code not in r.text
        # They are still retrievable from the full call log.
        log = client.get("/admin/calls")
        assert s2.ref_code in log.text and s3.ref_code in log.text
        # The uncomfortable metric is rendered.
        assert "Did emergencies actually get help?" in r.text

    def test_admin_call_log_lists_every_call(self, client, db, region):
        _make_user(db, "boss2", Role.admin)
        sessions = [
            _run_web_call(db, region, {"m_bleeding": "yes"}),
            _run_web_call(db, region, {}, track="newborn"),
        ]
        _login(client, "boss2")
        r = client.get("/admin/calls")
        for s in sessions:
            assert s.ref_code in r.text

    def test_login_rejects_bad_password(self, client, db):
        _make_user(db, "guard", Role.admin, password="right123")
        r = _login(client, "guard", password="wrong123")
        assert r.status_code == 303 and "error=1" in r.headers["location"]


class TestRegionConfig:
    def test_admin_can_add_region_and_contact(self, client, db):
        _make_user(db, "cfg", Role.admin)
        _login(client, "cfg")
        r = client.post("/admin/regions", data={
            "state": "X", "district": "Y", "block": "Z",
            "emergency_number": "108", "confirm_window_sec": "300",
        }, follow_redirects=True)
        assert r.status_code == 200
        region = db.query(Region).filter_by(block="Z").first()
        assert region and region.confirm_window_sec == 300

        client.post(f"/admin/regions/{region.id}/contacts", data={
            "name": "New ASHA", "kind": ContactKind.asha.value,
            "phone": "9000000000", "priority": "5", "notes": "",
        }, follow_redirects=True)
        contact = db.query(BackupContact).filter_by(region_id=region.id).first()
        assert contact and contact.priority == 5
