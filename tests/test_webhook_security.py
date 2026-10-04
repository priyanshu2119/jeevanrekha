"""Webhook hardening: authentication, DID->region resolution, replay dedupe
and Twilio signature validation.

These endpoints are the only public door into the triage/dispatch machinery.
An unauthenticated responder-confirm means anyone on the internet can tell a
panicking caller "ambulance confirmed" -- so every mechanism gets a proof.
"""
import base64
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import CallSession, IncidentLog, RegionPhoneNumber


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


class TestWebhookAuth:
    def test_open_in_dev_with_nothing_configured(self, client, db, region):
        # Zero-config local development must keep working.
        assert settings.WEBHOOK_SECRET == ""
        assert settings.WEBHOOK_ALLOWED_CIDRS == ""
        r = client.post("/webhooks/exotel/inbound",
                        data={"From": "9876500011", "Digits": "", "CallSid": "CA-dev"})
        assert r.status_code == 200
        assert "<Response>" in r.text

    def test_secret_required_when_configured(self, client, db, region, monkeypatch):
        monkeypatch.setattr(settings, "WEBHOOK_SECRET", "s3cret-token")
        r = client.post("/webhooks/exotel/inbound",
                        data={"From": "9876500011", "Digits": "", "CallSid": "CA-a"})
        assert r.status_code == 403

        r = client.post("/webhooks/exotel/inbound",
                        headers={"X-JR-Webhook-Secret": "s3cret-token"},
                        data={"From": "9876500011", "Digits": "", "CallSid": "CA-b"})
        assert r.status_code == 200

        # Query-param form (Exotel dashboard URLs cannot set headers).
        r = client.post("/webhooks/exotel/inbound?token=s3cret-token",
                        data={"From": "9876500012", "Digits": "", "CallSid": "CA-c"})
        assert r.status_code == 200

        # Wrong secret is still rejected.
        r = client.post("/webhooks/exotel/inbound",
                        headers={"X-JR-Webhook-Secret": "wrong"},
                        data={"From": "9876500013", "Digits": "", "CallSid": "CA-d"})
        assert r.status_code == 403

    def test_rejection_is_audited(self, client, db, region, monkeypatch):
        monkeypatch.setattr(settings, "WEBHOOK_SECRET", "s3cret-token")
        client.post("/webhooks/exotel/inbound", data={"From": "9876500011"})
        incident = db.scalars(select(IncidentLog).where(
            IncidentLog.kind == "webhook_rejected")).first()
        assert incident is not None
        assert "exotel" in incident.detail

    def test_cidr_allowlist(self, db, region, monkeypatch):
        monkeypatch.setattr(settings, "WEBHOOK_ALLOWED_CIDRS", "10.0.0.0/8")
        with TestClient(app, client=("10.0.0.7", 55555)) as allowed:
            r = allowed.post("/webhooks/exotel/inbound",
                             data={"From": "9876500011", "Digits": "", "CallSid": "CA-ok"})
            assert r.status_code == 200
        with TestClient(app, client=("192.168.1.5", 55555)) as denied:
            r = denied.post("/webhooks/exotel/inbound",
                            data={"From": "9876500011", "Digits": "", "CallSid": "CA-bad"})
            assert r.status_code == 403

    def test_responder_confirm_is_protected(self, client, db, region, monkeypatch):
        # The most dangerous endpoint: it can fake "help is coming".
        monkeypatch.setattr(settings, "WEBHOOK_SECRET", "s3cret-token")
        r = client.post("/webhooks/responder-confirm",
                        data={"phone": "9000000001", "digits": "1"})
        assert r.status_code == 403


class TestRegionResolution:
    def test_inbound_call_resolves_region_from_did(self, client, db, region):
        db.add(RegionPhoneNumber(region_id=region.id, provider="exotel",
                                 phone_number="0801234567"))
        db.commit()
        r = client.post("/webhooks/exotel/inbound",
                        data={"From": "9876500022", "To": "0801234567",
                              "CallSid": "CA-region", "Digits": "1"})
        assert r.status_code == 200
        session = db.scalars(select(CallSession).where(
            CallSession.caller_phone == "9876500022")).first()
        assert session is not None
        # Without this the backup chain (Track B) can never fire for a real
        # caller -- the whole point of the numbering plan.
        assert session.region_id == region.id

    def test_unknown_did_leaves_region_unresolved(self, client, db, region):
        r = client.post("/webhooks/exotel/inbound",
                        data={"From": "9876500033", "To": "0999999999",
                              "CallSid": "CA-unknown", "Digits": ""})
        assert r.status_code == 200
        session = db.scalars(select(CallSession).where(
            CallSession.caller_phone == "9876500033")).first()
        assert session.region_id is None


class TestReplayDedupe:
    def test_replayed_digit_is_not_recorded_twice(self, client, db, region):
        data = {"From": "9876500044", "CallSid": "CA-dedup", "Digits": "1"}
        client.post("/webhooks/exotel/inbound", data=data)   # selects Hindi
        client.post("/webhooks/exotel/inbound", data=data)   # provider retry
        session = db.scalars(select(CallSession).where(
            CallSession.caller_phone == "9876500044")).first()
        assert session.language == "hi"
        # A duplicated "1" must NOT have been consumed as the track answer
        # (maternal) -- the retry is ignored, the caller hears the prompt again.
        assert session.track is None

    def test_same_digit_accepted_after_window(self, client, db, region, monkeypatch):
        monkeypatch.setattr(settings, "WEBHOOK_DEDUP_SEC", 0)
        data = {"From": "9876500055", "CallSid": "CA-window", "Digits": "1"}
        client.post("/webhooks/exotel/inbound", data=data)  # language -> hi
        client.post("/webhooks/exotel/inbound", data=data)  # now a real press: track
        session = db.scalars(select(CallSession).where(
            CallSession.caller_phone == "9876500055")).first()
        assert session.language == "hi"
        assert session.track == "maternal"


class TestTwilioSignature:
    def _sign(self, token: str, url: str, params: dict) -> str:
        payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
        return base64.b64encode(
            hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
        ).decode()

    def test_valid_signature_accepted_invalid_rejected(self, client, db, region, monkeypatch):
        monkeypatch.setattr(settings, "TWILIO_TOKEN", "auth-token-123")
        params = {"From": "+919876500066", "To": "+91801234567",
                  "Digits": "1", "CallSid": "CA-sig"}
        url = "http://testserver/webhooks/twilio/inbound"

        good = self._sign("auth-token-123", url, params)
        r = client.post("/webhooks/twilio/inbound", data=params,
                        headers={"X-Twilio-Signature": good})
        assert r.status_code == 200

        bad = self._sign("some-other-token", url, params)
        r = client.post("/webhooks/twilio/inbound", data=params,
                        headers={"X-Twilio-Signature": bad})
        assert r.status_code == 403

        r = client.post("/webhooks/twilio/inbound", data=params)
        assert r.status_code == 403  # missing signature entirely
