"""Rate limiting: brute-force and abuse protection with provider webhooks
deliberately exempt (a rate-limited emergency call is a dropped emergency)."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.web import ratelimit


@pytest.fixture(autouse=True)
def _clean_buckets():
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


class TestRateLimit:
    def test_login_brute_force_is_throttled(self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(settings, "RATE_LIMIT_AUTH", "3/minute")
        for _ in range(3):
            r = client.post("/login", data={"username": "x", "password": "y",
                                            "csrf_token": ""})
            assert r.status_code != 429  # 403 (CSRF) or 303, but not limited yet
        r = client.post("/login", data={"username": "x", "password": "y",
                                        "csrf_token": ""})
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) >= 1

    def test_flow_start_is_throttled(self, client, db, region, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(settings, "RATE_LIMIT_FLOW_START", "2/minute")
        statuses = [
            client.post("/api/flow/start", json={"region_id": region.id}).status_code
            for _ in range(3)
        ]
        assert statuses[:2] == [200, 200]
        assert statuses[2] == 429

    def test_webhooks_are_exempt(self, client, db, region, monkeypatch):
        # Never rate-limit the PSTN door: a dropped webhook is a dropped call.
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(settings, "RATE_LIMIT_DEFAULT", "1/minute")
        for i in range(4):
            r = client.post("/webhooks/exotel/inbound",
                            data={"From": f"98765000{i:02d}", "Digits": "",
                                  "CallSid": f"CA-rl-{i}"})
            assert r.status_code == 200

    def test_health_probes_are_exempt(self, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(settings, "RATE_LIMIT_DEFAULT", "1/minute")
        for _ in range(5):
            assert client.get("/healthz").status_code == 200

    def test_disabled_by_flag(self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
        monkeypatch.setattr(settings, "RATE_LIMIT_AUTH", "1/minute")
        for _ in range(5):
            r = client.post("/login", data={"username": "x", "password": "y",
                                            "csrf_token": ""})
            assert r.status_code != 429
