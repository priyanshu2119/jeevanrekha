"""Runtime configuration.

Every knob that a district deployment might legitimately need to change lives
here and can be overridden by environment variables -- no code edits required
to retune escalation windows or swap the database for Postgres in production.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("JR_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings:
    # --- identity -----------------------------------------------------------
    SERVICE_NAME = "JeevanRekha"
    SERVICE_TAGLINE = "Maternal & newborn danger-sign triage and emergency routing"
    HELPLINE_DISPLAY = os.environ.get("JR_HELPLINE_DISPLAY", "1800-XXX-XXXX")

    # --- storage ------------------------------------------------------------
    DATABASE_URL = os.environ.get(
        "JR_DATABASE_URL", f"sqlite:///{DATA_DIR / 'jeevanrekha.db'}"
    )

    # --- security -----------------------------------------------------------
    SECRET_KEY = os.environ.get("JR_SECRET_KEY", "dev-only-insecure-key-change-me")
    SESSION_COOKIE = "jr_session"
    PBKDF2_ITERATIONS = 240_000

    # --- triage engine ------------------------------------------------------
    # Bump whenever rules.py or questions.py semantics change; stored on every
    # triage result so historical decisions remain interpretable.
    ENGINE_VERSION = "1.0.0"

    # --- dispatch / escalation ----------------------------------------------
    # Seconds to wait for a dispatch confirmation before escalating.
    # Default 8 minutes: well inside the 60-minute golden hour, leaving time
    # for the backup chain to actually move a patient.
    DISPATCH_CONFIRM_WINDOW_SEC = int(os.environ.get("JR_CONFIRM_WINDOW", "480"))
    # Maximum attempts per contact before moving to the next in the chain.
    DISPATCH_MAX_ATTEMPTS_PER_CONTACT = int(os.environ.get("JR_MAX_ATTEMPTS", "2"))
    # Scheduler tick interval.
    SCHEDULER_TICK_SEC = float(os.environ.get("JR_TICK", "2.0"))

    # --- telephony ----------------------------------------------------------
    # "simulated" (default, local), "exotel" or "twilio" (production adapters).
    TELEPHONY_PROVIDER = os.environ.get("JR_TELEPHONY", "simulated")
    EXOTEL_SID = os.environ.get("JR_EXOTEL_SID", "")
    EXOTEL_TOKEN = os.environ.get("JR_EXOTEL_TOKEN", "")
    EXOTEL_SUBDOMAIN = os.environ.get("JR_EXOTEL_SUBDOMAIN", "api")
    TWILIO_SID = os.environ.get("JR_TWILIO_SID", "")
    TWILIO_TOKEN = os.environ.get("JR_TWILIO_TOKEN", "")
    TWILIO_FROM = os.environ.get("JR_TWILIO_FROM", "")

    # --- ivr timing (simulator + real providers) -----------------------------
    IVR_NO_INPUT_REPEATS = 1  # after this many silent repeats -> record UNCLEAR
    IVR_MAX_INVALID = 1       # after this many invalid keys -> record UNCLEAR


settings = Settings()
