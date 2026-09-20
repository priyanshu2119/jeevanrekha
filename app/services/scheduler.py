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

from ..config import settings
from ..db import SessionLocal
from . import call_flow, dispatch

log = logging.getLogger("jr.scheduler")

_task: asyncio.Task | None = None


async def _loop() -> None:
    log.info("scheduler started (tick=%.1fs)", settings.SCHEDULER_TICK_SEC)
    while True:
        try:
            await asyncio.sleep(settings.SCHEDULER_TICK_SEC)
            db = SessionLocal()
            try:
                n = dispatch.process_timeouts(db)
                m = call_flow.finalise_idle_sessions(db)
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
