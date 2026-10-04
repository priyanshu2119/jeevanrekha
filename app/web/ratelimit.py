"""Per-IP sliding-window rate limiting (pure ASGI).

Deliberately dependency-free and in-process: each worker keeps its own
counters, so with N workers the effective ceiling is N x the configured limit
-- acceptable for a pilot, and the real edge defence in production is the
reverse proxy (nginx/Traefik can enforce per-IP limits before traffic even
reaches the app). What this layer guarantees is that a single misbehaving or
malicious client cannot spin unbounded triage sessions or brute-force the
staff login, which matters because every /api/flow/start writes rows and every
login attempt burns ~100ms of PBKDF2 CPU.

Implemented as PURE ASGI middleware (not BaseHTTPMiddleware) so it never
wraps or buffers response bodies: the SSE live-status feed and client
disconnect propagation must behave exactly as if the middleware were not
there.

Exempt by design:
* /webhooks/*   -- authenticated separately (secret/CIDR/signature); rate
                   limiting them could drop a real emergency call.
* /healthz, /readyz -- probes hit these constantly.
* /static/*     -- cheap, cacheable assets.
"""
from __future__ import annotations

import logging
import time
from collections import deque

from fastapi.responses import JSONResponse

from ..config import settings

log = logging.getLogger("jr.ratelimit")

# (bucket, client_ip) -> timestamps (monotonic seconds) inside the window.
_buckets: dict[tuple[str, str], deque[float]] = {}
_MAX_KEYS = 20_000  # cheap memory guard; pruned lazily


def _parse_spec(spec: str) -> tuple[int, float]:
    """'20/minute' -> (20, 60.0). Falls back to permissive on garbage."""
    try:
        count_s, _, unit = spec.partition("/")
        count = int(count_s)
        window = {
            "second": 1.0, "minute": 60.0, "hour": 3600.0, "day": 86400.0,
        }.get(unit.strip().lower(), 60.0)
        return count, window
    except (ValueError, AttributeError):
        return 10_000, 60.0


def _classify(path: str, method: str) -> str | None:
    if (
        path.startswith("/static")
        or path in ("/healthz", "/readyz")
        or path.startswith("/webhooks")
    ):
        return None
    if path in ("/login", "/api/auth/login") and method == "POST":
        return "auth"
    if path == "/api/flow/start" and method == "POST":
        return "flow_start"
    if path.startswith("/api/") or method == "POST":
        return "default"
    return None  # plain HTML GETs are cheap and often proxied/cached


def reset() -> None:
    """Clear all counters (tests, or after a config reload)."""
    _buckets.clear()


class RateLimitMiddleware:
    """Pure ASGI: inspects scope only, streams everything else through."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.RATE_LIMIT_ENABLED:
            await self.app(scope, receive, send)
            return

        bucket = _classify(scope.get("path", ""), scope.get("method", "GET"))
        if bucket is None:
            await self.app(scope, receive, send)
            return

        spec = getattr(settings, f"RATE_LIMIT_{bucket.upper()}",
                       settings.RATE_LIMIT_DEFAULT)
        limit, window = _parse_spec(spec)
        client = scope.get("client") or ("unknown", 0)
        key = (bucket, client[0])
        now = time.monotonic()

        stamps = _buckets.get(key)
        if stamps is None:
            if len(_buckets) > _MAX_KEYS:
                for k in [k for k, v in _buckets.items()
                          if not v or now - v[-1] > window]:
                    _buckets.pop(k, None)
            stamps = _buckets.setdefault(key, deque())

        while stamps and now - stamps[0] > window:
            stamps.popleft()

        if len(stamps) >= limit:
            retry_after = max(1, int(window - (now - stamps[0])) + 1)
            log.warning("rate limit %s exceeded for %s (%s)",
                        bucket, client[0], scope.get("path"))
            response = JSONResponse(
                {"error": "rate_limited",
                 "detail": "too many requests, slow down"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        stamps.append(now)
        await self.app(scope, receive, send)
