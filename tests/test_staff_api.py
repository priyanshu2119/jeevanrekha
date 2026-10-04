"""Staff JSON API for the mobile apps: token auth, device registry,
role-scoped live overview, call detail, and desk actions from the field."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.engine.questions import questions_for
from app.main import app
from app.models import (
    CallSession,
    Channel,
    DeviceToken,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    DispatchStatus,
    DispatchTrack,
    Region,
    Role,
    Tier,
    User,
)
from app.security import hash_password
from app.services import call_flow, push


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


def _make_user(db, username, role, region=None, password="pw123456"):
    pw, salt = hash_password(password)
    u = User(username=username, full_name=username.title(), role=role.value,
             region_id=region.id if region else None,
             password_hash=pw, salt=salt)
    db.add(u)
    db.commit()
    return u


def _login(client, username, password="pw123456"):
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _run_emergency(db, region, phone="9800010001"):
    session = call_flow.start_call(db, channel=Channel.web,
                                   region_id=region.id, caller_phone=phone)
    db.commit()
    call_flow.handle_digit(db, session, "2")   # english
    db.commit()
    call_flow.handle_digit(db, session, "1")   # maternal
    db.commit()
    for q in questions_for("maternal"):
        call_flow.handle_digit(db, session, "1" if q.id == "m_bleeding" else "2")
        db.commit()
    db.refresh(session)
    assert session.result.tier == Tier.emergency.value
    return session


def _second_region(db):
    other = Region(state="Other", district="Other", block="Other",
                   emergency_number="108")
    db.add(other)
    db.commit()
    return other


class TestTokenAuth:
    def test_login_returns_token_and_user(self, client, db):
        _make_user(db, "asha-api", Role.asha)
        r = client.post("/api/auth/login",
                        json={"username": "asha-api", "password": "pw123456"})
        assert r.status_code == 200
        body = r.json()
        assert body["token"]
        assert body["user"]["username"] == "asha-api"
        assert body["user"]["role"] == "asha"

    def test_login_rejects_bad_password(self, client, db):
        _make_user(db, "guard2", Role.admin)
        r = client.post("/api/auth/login",
                        json={"username": "guard2", "password": "wrong"})
        assert r.status_code == 401

    def test_bearer_token_grants_access(self, client, db):
        _make_user(db, "op-api", Role.operator)
        token = _login(client, "op-api")
        r = client.get("/api/staff/overview", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "operator"

    def test_no_credentials_is_401(self, client, db):
        assert client.get("/api/staff/overview").status_code == 401


class TestDeviceRegistry:
    def test_register_upsert_and_unregister(self, client, db):
        _make_user(db, "asha-dev", Role.asha)
        token = _login(client, "asha-dev")
        r = client.post("/api/devices", headers=_auth(token),
                        json={"token": "fcm-abc", "label": "Redmi 9"})
        assert r.json()["ok"] is True
        row = db.scalars(select(DeviceToken).where(
            DeviceToken.token == "fcm-abc")).first()
        assert row and row.is_active and row.label == "Redmi 9"

        # Re-registering the same device token keeps one row.
        client.post("/api/devices", headers=_auth(token),
                    json={"token": "fcm-abc"})
        rows = db.scalars(select(DeviceToken).where(
            DeviceToken.token == "fcm-abc")).all()
        assert len(rows) == 1

        r = client.request("DELETE", "/api/devices", headers=_auth(token),
                           json={"token": "fcm-abc"})
        assert r.json()["ok"] is True
        db.refresh(row)
        assert not row.is_active

    def test_device_rebinds_to_current_user(self, client, db):
        # A handed-down phone must stop alerting the previous owner.
        _make_user(db, "old-owner", Role.asha)
        _make_user(db, "new-owner", Role.asha)
        old = _login(client, "old-owner")
        client.post("/api/devices", headers=_auth(old), json={"token": "fcm-x"})
        new = _login(client, "new-owner")
        client.post("/api/devices", headers=_auth(new), json={"token": "fcm-x"})
        row = db.scalars(select(DeviceToken).where(
            DeviceToken.token == "fcm-x")).first()
        new_user = db.scalars(select(User).where(
            User.username == "new-owner")).first()
        assert row.user_id == new_user.id


class TestOverviewScoping:
    def test_asha_sees_only_her_region(self, client, db, region):
        other = _second_region(db)
        _make_user(db, "asha-mine", Role.asha, region=region)
        s1 = _run_emergency(db, region, phone="9800020001")
        s2 = _run_emergency(db, other, phone="9800020002")

        token = _login(client, "asha-mine")
        body = client.get("/api/staff/overview", headers=_auth(token)).json()
        refs = {c["call_ref"] for c in body["active_cases"]}
        assert s1.ref_code in refs
        assert s2.ref_code not in refs
        recent_refs = {c["ref"] for c in body["recent_calls"]}
        assert s2.ref_code not in recent_refs

    def test_operator_sees_everything_and_the_followup_queue(self, client, db, region):
        other = _second_region(db)
        _make_user(db, "op-all", Role.operator)
        s1 = _run_emergency(db, region, phone="9800030001")
        s2 = _run_emergency(db, other, phone="9800030002")

        token = _login(client, "op-all")
        body = client.get("/api/staff/overview", headers=_auth(token)).json()
        refs = {c["call_ref"] for c in body["active_cases"]}
        assert {s1.ref_code, s2.ref_code} <= refs
        # Both tracks rang for the in-region case.
        case = next(c for c in body["active_cases"] if c["call_ref"] == s1.ref_code)
        assert case["ambulance"]["state"] == "waiting"
        assert case["backup"]["state"] == "waiting"
        assert body["pending_alerts"]

    def test_asha_without_region_sees_nothing(self, client, db, region):
        _make_user(db, "asha-none", Role.asha, region=None)
        _run_emergency(db, region, phone="9800040001")
        token = _login(client, "asha-none")
        body = client.get("/api/staff/overview", headers=_auth(token)).json()
        assert body["active_cases"] == []
        assert body["pending_alerts"] == []


class TestDeskActions:
    def _pending_backup(self, db):
        return db.scalars(select(DispatchEvent).where(
            DispatchEvent.track == DispatchTrack.backup.value,
            DispatchEvent.action == DispatchAction.call_placed.value,
            DispatchEvent.resolved.is_(False))).first()

    def test_asha_confirms_alert_for_her_region(self, client, db, region):
        _make_user(db, "asha-act", Role.asha, region=region)
        session = _run_emergency(db, region, phone="9800050001")
        ev = self._pending_backup(db)
        token = _login(client, "asha-act")
        r = client.post(f"/api/desk/{ev.id}/confirm", headers=_auth(token))
        assert r.json()["ok"] is True
        case = db.get(DispatchCase, ev.case_id)
        db.refresh(case)
        assert case.status == DispatchStatus.confirmed.value
        # The confirmation is attributed to the app user in the audit trail.
        conf = db.scalars(select(DispatchEvent).where(
            DispatchEvent.case_id == case.id,
            DispatchEvent.action == DispatchAction.confirmed.value)).first()
        assert "app:asha-act" in conf.detail

    def test_asha_cannot_act_on_another_region(self, client, db, region):
        other = _second_region(db)
        _make_user(db, "asha-far", Role.asha, region=other)
        _run_emergency(db, region, phone="9800060001")
        ev = self._pending_backup(db)
        token = _login(client, "asha-far")
        r = client.post(f"/api/desk/{ev.id}/confirm", headers=_auth(token))
        assert r.status_code == 403

    def test_asha_decline_escalates(self, client, db, region):
        _make_user(db, "asha-dec", Role.asha, region=region)
        _run_emergency(db, region, phone="9800070001")
        ev = self._pending_backup(db)
        token = _login(client, "asha-dec")
        r = client.post(f"/api/desk/{ev.id}/decline", headers=_auth(token))
        assert r.json()["ok"] is True
        db.refresh(ev)
        assert ev.resolved

    def test_operator_ack_and_asha_forbidden(self, client, db):
        # A region with NO backup contacts raises an operator alert at once.
        lonely = Region(state="Lonely", district="L", block="L",
                        emergency_number="108")
        db.add(lonely)
        db.commit()
        _make_user(db, "op-ack", Role.operator)
        _make_user(db, "asha-ack", Role.asha, region=lonely)
        _run_emergency(db, lonely, phone="9800080001")
        op_ev = db.scalars(select(DispatchEvent).where(
            DispatchEvent.track == DispatchTrack.operator.value,
            DispatchEvent.action == DispatchAction.operator_alert.value,
            DispatchEvent.resolved.is_(False))).first()
        assert op_ev is not None

        asha_token = _login(client, "asha-ack")
        r = client.post(f"/api/desk/{op_ev.id}/ack", headers=_auth(asha_token))
        assert r.status_code == 403

        op_token = _login(client, "op-ack")
        r = client.post(f"/api/desk/{op_ev.id}/ack", headers=_auth(op_token))
        assert r.json()["ok"] is True
        db.refresh(op_ev)
        assert op_ev.resolved


class TestCallDetail:
    def test_detail_json_has_answers_result_timeline(self, client, db, region):
        _make_user(db, "op-detail", Role.operator)
        session = _run_emergency(db, region, phone="9800090001")
        token = _login(client, "op-detail")
        r = client.get(f"/api/staff/calls/{session.ref_code}",
                       headers=_auth(token))
        assert r.status_code == 200
        body = r.json()
        assert body["call"]["ref"] == session.ref_code
        assert body["result"]["tier"] == "emergency"
        assert "bleeding" in body["result"]["flagged"]
        assert len(body["answers"]) == len(questions_for("maternal"))
        actions = {t["action"] for t in body["timeline"]}
        assert DispatchAction.call_placed.value in actions
        assert body["dispatch"]["status"] == "active"

    def test_out_of_scope_call_is_not_found(self, client, db, region):
        other = _second_region(db)
        _make_user(db, "asha-scope", Role.asha, region=other)
        session = _run_emergency(db, region, phone="9800100001")
        token = _login(client, "asha-scope")
        r = client.get(f"/api/staff/calls/{session.ref_code}",
                       headers=_auth(token))
        assert r.status_code == 404


class TestPushSafety:
    def test_noop_without_fcm_credentials(self, db, region):
        assert not push.enabled()
        session = _run_emergency(db, region, phone="9800110001")
        # Must be a silent no-op, never an exception into dispatch.
        push.notify_case_event(db, session, "dispatch_alert", high_priority=True)

    def test_targets_scoped_to_region_staff(self, db, region):
        other = _second_region(db)
        mine = _make_user(db, "asha-t", Role.asha, region=region)
        theirs = _make_user(db, "asha-o", Role.asha, region=other)
        op = _make_user(db, "op-t", Role.operator)
        session = _run_emergency(db, region, phone="9800120001")
        targets = push.targets_for_case(db, session)
        assert mine.id in targets and op.id in targets
        assert theirs.id not in targets

    def test_dispatch_survives_broken_fcm_credentials(self, db, region, monkeypatch):
        # Credentials configured but unreadable: push fails silently and the
        # confirmation still lands -- dispatch must never depend on push.
        monkeypatch.setattr(settings, "FCM_SERVICE_ACCOUNT",
                            "/nonexistent/service-account.json")
        session = _run_emergency(db, region, phone="9800130001")
        ev = db.scalars(select(DispatchEvent).where(
            DispatchEvent.track == DispatchTrack.backup.value,
            DispatchEvent.action == DispatchAction.call_placed.value,
            DispatchEvent.resolved.is_(False))).first()
        case = db.get(DispatchCase, ev.case_id)
        from app.services import dispatch as dispatch_service
        dispatch_service.confirm(db, case, ev.track, by="test")
        db.commit()
        db.refresh(case)
        assert case.status == DispatchStatus.confirmed.value
