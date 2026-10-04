"""Standalone escalation-scheduler process.

Why this exists: the scheduler is an in-process asyncio task. Run
``uvicorn --workers 4`` and every worker escalates every timeout -- four
duplicate outbound calls to an ambulance node for one emergency. The fix in
production is to serve web traffic with N workers (JR_RUN_SCHEDULER=false) and
run exactly ONE instance of this process alongside them::

    python -m app.services.scheduler_worker

Escalation timing lives in the database (dispatch_events.due_at), not in
memory, so restarting this worker mid-emergency loses nothing: the next tick
picks up every overdue attempt. SIGTERM/SIGINT cancel cleanly.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from ..config import settings
from ..db import init_db
from . import scheduler

log = logging.getLogger("jr.scheduler_worker")


async def _amain() -> None:
    settings.validate_runtime()
    init_db()
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, main_task.cancel)
        except (NotImplementedError, RuntimeError):
            pass  # platform without unix signal handlers
    log.info(
        "standalone scheduler worker starting (tick=%.1fs, provider=%s)",
        settings.SCHEDULER_TICK_SEC, settings.TELEPHONY_PROVIDER,
    )
    await scheduler._loop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_amain())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("scheduler worker stopped")


if __name__ == "__main__":
    main()
