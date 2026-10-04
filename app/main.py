"""JeevanRekha application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .config import settings
from .db import init_db
from .services import scheduler
from .web import deps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("jr.app")

HERE = Path(__file__).resolve().parent

# Optional error tracking. Only active when JR_SENTRY_DSN is set; the import
# is guarded so the app runs fine without sentry-sdk installed.
if settings.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            integrations=[StarletteIntegration(), FastApiIntegration()],
        )
        log.info("sentry error tracking enabled")
    except ImportError:
        log.warning("JR_SENTRY_DSN is set but sentry-sdk is not installed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime()
    init_db()
    if settings.RUN_SCHEDULER:
        scheduler.start()
    else:
        # Multi-worker deployment: exactly one dedicated process must run
        # `python -m app.services.scheduler_worker`, or dispatch timeouts
        # will never escalate. /readyz reports the heartbeat either way.
        log.warning("in-process scheduler DISABLED (JR_RUN_SCHEDULER=false); "
                    "a dedicated scheduler worker must be running")
    log.info(
        "%s ready (env=%s, provider=%s, engine=%s, confirm window=%ss, scheduler=%s)",
        settings.SERVICE_NAME, settings.ENV, settings.TELEPHONY_PROVIDER,
        settings.ENGINE_VERSION, settings.DISPATCH_CONFIRM_WINDOW_SEC,
        "in-process" if settings.RUN_SCHEDULER else "external",
    )
    yield
    await scheduler.stop()


app = FastAPI(
    title=settings.SERVICE_NAME,
    description=settings.SERVICE_TAGLINE,
    version=settings.ENGINE_VERSION,
    lifespan=lifespan,
    docs_url=None,       # this is a life-safety service, not a public API playground
    redoc_url=None,
)

app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

from .web import health, public, sim, staff, staff_api, webhooks  # noqa: E402  (after app exists)
from .web.ratelimit import RateLimitMiddleware  # noqa: E402

# Pure ASGI (never BaseHTTPMiddleware) so the SSE live-status feed streams
# and cancels exactly as if it were not there.
app.add_middleware(RateLimitMiddleware)

app.include_router(health.router)
app.include_router(public.router)
app.include_router(sim.router)
app.include_router(staff.router)
app.include_router(staff_api.router)
app.include_router(webhooks.router)

deps.init_templates(HERE / "templates")
