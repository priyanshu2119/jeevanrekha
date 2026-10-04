"""Liveness/readiness probes and the scheduler heartbeat they surface."""
import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import SystemStatus
from app.services import scheduler


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


class TestProbes:
    def test_healthz_is_always_ok(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "JeevanRekha"

    def test_readyz_reports_db_ok(self, client):
        r = client.get("/readyz")
        assert r.status_code == 200
        body = r.json()
        assert body["db"] == "ok"

    def test_readyz_reports_missing_heartbeat_without_failing(self, client):
        # Web workers must not go unready just because the (separate)
        # scheduler has not written a heartbeat yet -- but the absence is
        # surfaced honestly.
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["scheduler"] == {"heartbeat": "never"}


class TestSchedulerHeartbeat:
    def test_heartbeat_row_written(self, db):
        session = SessionLocal()
        try:
            scheduler._write_heartbeat(session)
            session.commit()
            row = session.get(SystemStatus, "scheduler")
            assert row is not None
            assert row.payload.get("pid")
            assert row.updated_at is not None
        finally:
            session.close()

    def test_readyz_shows_heartbeat_age(self, client, db):
        session = SessionLocal()
        try:
            scheduler._write_heartbeat(session)
            session.commit()
        finally:
            session.close()
        r = client.get("/readyz")
        assert r.status_code == 200
        sched = r.json()["scheduler"]
        assert sched["heartbeat_age_sec"] < 60
        assert sched["stale"] is False
