"""Durable background worker.

Escalation timing lives in the database (``dispatch_events.due_at``), not in
memory, so timeouts survive process restarts -- a half-escalated emergency
must not be lost because a deploy happened. The worker is a single asyncio
task that ticks every ``SCHEDULER_TICK_SEC`` seconds and:

1. resolves dispatch attempts whose confirmation window closed,
2. finalises sessions that went idle mid-triage (watchdog).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from ..config import settings
from ..db import SessionLocal
from ..models import SystemStatus, utcnow
from . import call_flow, dispatch

log = logging.getLogger("jr.scheduler")

_task: asyncio.Task | None = None

# Heartbeat: written at most every N seconds (not every tick -- a 2s write
# loop would churn the WAL for no benefit). /readyz and monitoring read it;
# a stale heartbeat means escalations are NOT being processed, which for this
# service is the one silence that must never happen.
_HEARTBEAT_EVERY_SEC = 5.0
_last_heartbeat = 0.0


def _write_heartbeat(db) -> None:
    payload = {"pid": os.getpid(), "tick_sec": settings.SCHEDULER_TICK_SEC}
    row = db.get(SystemStatus, "scheduler")
    if row is None:
        db.add(SystemStatus(key="scheduler", payload=payload))
    else:
        row.payload = payload
        row.updated_at = utcnow()


async def _loop() -> None:
    global _last_heartbeat
    log.info("scheduler started (tick=%.1fs)", settings.SCHEDULER_TICK_SEC)
    while True:
        try:
            await asyncio.sleep(settings.SCHEDULER_TICK_SEC)
            db = SessionLocal()
            try:
                n = dispatch.process_timeouts(db)
                m = call_flow.finalise_idle_sessions(db)
                now = time.monotonic()
                if now - _last_heartbeat >= _HEARTBEAT_EVERY_SEC:
                    _write_heartbeat(db)
                    _last_heartbeat = now
                db.commit()
                if n or m:
                    log.info("tick: %s timeout(s) escalated, %s idle session(s) finalised", n, m)
            finally:
                db.close()
        except asyncio.CancelledError:
            log.info("scheduler stopping")
            raise
        except Exception:  # noqa: BLE001 - the worker must never die
            log.exception("scheduler tick failed")


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
