"""Runtime configuration.

Every knob that a district deployment might legitimately need to change lives
here and can be overridden by environment variables -- no code edits required
to retune escalation windows or swap the database for Postgres in production.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("JR_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("jr.config")

_INSECURE_DEFAULT_KEY = "dev-only-insecure-key-change-me"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # --- identity -----------------------------------------------------------
    SERVICE_NAME = "JeevanRekha"
    SERVICE_TAGLINE = "Maternal & newborn danger-sign triage and emergency routing"
    HELPLINE_DISPLAY = os.environ.get("JR_HELPLINE_DISPLAY", "1800-XXX-XXXX")

    # development | production. Production refuses to boot with the insecure
    # default SECRET_KEY or with unauthenticated telephony webhooks.
    ENV = os.environ.get("JR_ENV", "development").strip().lower()

    # Public origin of this service (e.g. https://api.jeevanrekha.example).
    # Used to rebuild absolute webhook URLs (provider signature validation,
    # ExoML endpoints) when running behind a TLS-terminating proxy.
    PUBLIC_BASE_URL = os.environ.get("JR_PUBLIC_BASE_URL", "").rstrip("/")

    # --- storage ------------------------------------------------------------
    DATABASE_URL = os.environ.get(
        "JR_DATABASE_URL", f"sqlite:///{DATA_DIR / 'jeevanrekha.db'}"
    )

    # --- security -----------------------------------------------------------
    SECRET_KEY = os.environ.get("JR_SECRET_KEY", _INSECURE_DEFAULT_KEY)
    SESSION_COOKIE = "jr_session"
    CSRF_COOKIE = "jr_csrf"
    PBKDF2_ITERATIONS = 240_000
    # Double-submit CSRF protection for cookie-authenticated form POSTs.
    CSRF_ENABLED = _env_bool("JR_CSRF_ENABLED", True)
    # Secure cookie flag: defaults to on in production, off in development
    # (local http://127.0.0.1 has no TLS and browsers reject Secure there).
    COOKIE_SECURE = _env_bool("JR_COOKIE_SECURE", default=(ENV == "production"))

    # --- telephony webhook authentication ------------------------------------
    # Shared secret a provider webhook must present (header
    # X-JR-Webhook-Secret or ?token= query param -- Exotel dashboard-configured
    # URLs cannot set custom headers, so the query form matters). Empty =
    # unauthenticated (local/simulated only; production refuses to boot).
    WEBHOOK_SECRET = os.environ.get("JR_WEBHOOK_SECRET", "")
    # Comma-separated CIDRs allowed to reach /webhooks/* (Exotel's documented
    # best practice is source-IP validation; Twilio additionally signs every
    # request and that signature is verified separately). Empty = allow any
    # source. Behind a proxy, run uvicorn with --proxy-headers so the real
    # client IP is used.
    WEBHOOK_ALLOWED_CIDRS = os.environ.get("JR_WEBHOOK_CIDRS", "")
    # Providers retry webhooks that do not answer in time (Exotel: up to 2
    # retries). A retry re-delivers the same DTMF digit; without dedupe the
    # duplicate would be recorded as an answer to the NEXT question -- an
    # unintended "no" could under-escalate, which is the dangerous direction.
    # Identical (CallSid, digit) pairs inside this window are ignored; the
    # caller simply hears the prompt again and re-presses (fail-safe). Kept
    # short (4s) because a distressed caller may legitimately press the same
    # digit for consecutive questions within a few seconds -- a dropped
    # repeat only ever costs time, never safety.
    WEBHOOK_DEDUP_SEC = int(os.environ.get("JR_WEBHOOK_DEDUP", "4"))

    # --- rate limiting (per client IP, sliding window, per worker process) ----
    RATE_LIMIT_ENABLED = _env_bool("JR_RATE_LIMIT_ENABLED", True)
    RATE_LIMIT_DEFAULT = os.environ.get("JR_RATE_LIMIT_DEFAULT", "240/minute")
    RATE_LIMIT_AUTH = os.environ.get("JR_RATE_LIMIT_AUTH", "20/minute")
    RATE_LIMIT_FLOW_START = os.environ.get("JR_RATE_LIMIT_FLOW_START", "30/minute")

    # --- observability --------------------------------------------------------
    SENTRY_DSN = os.environ.get("JR_SENTRY_DSN", "")
    SENTRY_TRACES_SAMPLE_RATE = float(os.environ.get("JR_SENTRY_TRACES", "0.1"))
    # A scheduler heartbeat older than this is reported stale by /readyz.
    HEARTBEAT_STALE_SEC = int(os.environ.get("JR_HEARTBEAT_STALE", "60"))
    # When true, /readyz fails (503) if the scheduler heartbeat is stale.
    # Enable on the dedicated scheduler container; leave off on web workers.
    READYZ_REQUIRE_SCHEDULER = _env_bool("JR_READYZ_REQUIRE_SCHEDULER", False)

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
    # Run the in-process escalation scheduler in THIS process. Must be false
    # on web workers when a dedicated scheduler process runs alongside --
    # otherwise every worker escalates every timeout (duplicate outbound
    # calls). Single-process deployments (run.sh, tests) keep the default.
    RUN_SCHEDULER = _env_bool("JR_RUN_SCHEDULER", True)

    # --- telephony ----------------------------------------------------------
    # "simulated" (default, local), "exotel" or "twilio" (production adapters).
    TELEPHONY_PROVIDER = os.environ.get("JR_TELEPHONY", "simulated")
    EXOTEL_SID = os.environ.get("JR_EXOTEL_SID", "")
    EXOTEL_TOKEN = os.environ.get("JR_EXOTEL_TOKEN", "")
    EXOTEL_SUBDOMAIN = os.environ.get("JR_EXOTEL_SUBDOMAIN", "api")
    EXOTEL_FROM = os.environ.get("JR_EXOTEL_FROM", "")
    # DLT (TRAI) compliance for SMS in India: messages must reference a
    # registered content template and Principal Entity. Exotel's Sms/send
    # accepts both; without them carriers drop the SMS.
    EXOTEL_DLT_TEMPLATE_ID = os.environ.get("JR_EXOTEL_DLT_TEMPLATE", "")
    EXOTEL_DLT_ENTITY_ID = os.environ.get("JR_EXOTEL_DLT_ENTITY", "")
    TWILIO_SID = os.environ.get("JR_TWILIO_SID", "")
    TWILIO_TOKEN = os.environ.get("JR_TWILIO_TOKEN", "")
    TWILIO_FROM = os.environ.get("JR_TWILIO_FROM", "")

    # --- ivr timing (simulator + real providers) -----------------------------
    IVR_NO_INPUT_REPEATS = 1  # after this many silent repeats -> record UNCLEAR
    IVR_MAX_INVALID = 1       # after this many invalid keys -> record UNCLEAR

    # --- startup validation ---------------------------------------------------
    def validate_runtime(self) -> None:
        """Fail fast on configurations that must never serve real traffic.

        Called from the application lifespan and the standalone scheduler
        worker. In development everything stays permissive so the local
        simulator keeps working with zero configuration.
        """
        if self.ENV != "production":
            return
        if self.SECRET_KEY == _INSECURE_DEFAULT_KEY:
            raise RuntimeError(
                "JR_ENV=production but JR_SECRET_KEY is still the insecure "
                "default. Generate one: python -c \"import secrets;"
                "print(secrets.token_hex(32))\""
            )
        if self.TELEPHONY_PROVIDER != "simulated" and not (
            self.WEBHOOK_SECRET or self.WEBHOOK_ALLOWED_CIDRS
        ):
            raise RuntimeError(
                "JR_ENV=production with a real telephony provider but no "
                "webhook authentication: set JR_WEBHOOK_SECRET and/or "
                "JR_WEBHOOK_CIDRS. Unauthenticated webhooks would let anyone "
                "on the internet confirm or decline a real emergency dispatch."
            )
        if self.TELEPHONY_PROVIDER != "simulated" and not self.PUBLIC_BASE_URL:
            raise RuntimeError(
                "JR_ENV=production with a real telephony provider but no "
                "JR_PUBLIC_BASE_URL: ExoML/TwiML action URLs and outbound "
                "call flows must be absolute and publicly reachable."
            )
        if not self.COOKIE_SECURE:
            log.warning("JR_COOKIE_SECURE is off in production; session cookies "
                        "can leak over plain HTTP")
        if self.DATABASE_URL.startswith("sqlite"):
            log.warning("SQLite in production: fine for a single-process pilot, "
                        "but move to Postgres (JR_DATABASE_URL) before scaling "
                        "out workers")


settings = Settings()
