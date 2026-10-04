"""Liveness and readiness probes.

* ``/healthz`` -- liveness. No dependencies touched: if this stops answering,
  the process is wedged and the orchestrator should restart it.
* ``/readyz``  -- readiness. Proves the database answers and reports the age
  of the escalation scheduler's heartbeat. A dispatch system whose escalator
  is silently dead is the one failure this service must never have, so the
  heartbeat is surfaced here (and can be made fatal to readiness with
  JR_READYZ_REQUIRE_SCHEDULER=true on the scheduler container).
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import SystemStatus, utcnow

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {"status": "ok", "service": settings.SERVICE_NAME,
            "version": settings.ENGINE_VERSION}


@router.get("/readyz")
def readyz(db: Session = Depends(get_db)):
    body: dict = {"status": "ok", "db": "ok"}
    status_code = 200
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - probe must report, not raise
        return JSONResponse({"status": "unavailable", "db": "error"}, status_code=503)

    beat = db.get(SystemStatus, "scheduler")
    if beat is None:
        body["scheduler"] = {"heartbeat": "never"}
        stale = settings.READYZ_REQUIRE_SCHEDULER
    else:
        age = (utcnow() - beat.updated_at).total_seconds()
        stale = age > settings.HEARTBEAT_STALE_SEC
        body["scheduler"] = {
            "heartbeat_age_sec": round(age, 1),
            "stale": stale,
            "pid": (beat.payload or {}).get("pid"),
        }
    if stale:
        body["status"] = "degraded"
        if settings.READYZ_REQUIRE_SCHEDULER:
            status_code = 503
    return JSONResponse(body, status_code=status_code)
