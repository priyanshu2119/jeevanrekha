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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    log.info(
        "%s ready (provider=%s, engine=%s, confirm window=%ss)",
        settings.SERVICE_NAME, settings.TELEPHONY_PROVIDER,
        settings.ENGINE_VERSION, settings.DISPATCH_CONFIRM_WINDOW_SEC,
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

from .web import public, sim, staff, webhooks  # noqa: E402  (after app exists)

app.include_router(public.router)
app.include_router(sim.router)
app.include_router(staff.router)
app.include_router(webhooks.router)

deps.init_templates(HERE / "templates")
