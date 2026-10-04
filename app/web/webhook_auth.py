"""Telephony webhook authentication.

A webhook is the only door through which the outside PSTN can drive the triage
state machine and -- far more dangerously -- confirm or decline a live
emergency dispatch. Leaving it open means anyone on the internet can POST
``responder-confirm`` and make this service tell a panicking caller that help
is coming when it is not. So every webhook request must pass at least one
configured mechanism:

1. **Shared secret** (``JR_WEBHOOK_SECRET``) -- presented as the
   ``X-JR-Webhook-Secret`` header or a ``?token=`` query parameter. Exotel
   configures webhook URLs from its dashboard and cannot attach custom
   headers, so the query form is the practical one there; keep the URL out of
   logs and rotate the secret if it leaks.
2. **Source IP allowlist** (``JR_WEBHOOK_CIDRS``) -- Exotel's own documented
   best practice is to "verify webhook requests originate from Exotel's IP
   ranges" (they do not sign webhooks). Run uvicorn with ``--proxy-headers``
   behind your TLS proxy so ``request.client`` carries the real source IP.
3. **Twilio request signature** -- Twilio signs every webhook with
   HMAC-SHA1(auth_token, full_url + sorted POST name+value pairs), base64'd
   into ``X-Twilio-Signature``. Verified per
   https://www.twilio.com/docs/usage/security#validating-requests when the
   provider is twilio and ``JR_TWILIO_TOKEN`` is set.

When nothing is configured the request is allowed (local simulator and tests
need zero-config), but ``settings.validate_runtime()`` refuses to boot a
production deployment with a real provider and no webhook auth at all.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging

from fastapi import Request

from ..config import settings

log = logging.getLogger("jr.webhook_auth")


def _client_ip(request: Request) -> str | None:
    if request.client and request.client.host:
        return request.client.host
    return None


def _cidr_allowed(ip: str | None) -> bool:
    networks = [
        n.strip() for n in settings.WEBHOOK_ALLOWED_CIDRS.split(",") if n.strip()
    ]
    if not networks:
        return True  # not configured -> mechanism inactive
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False  # unparseable source (e.g. unix socket) -> deny
    return any(addr in ipaddress.ip_network(n, strict=False) for n in networks)


def _secret_presented(request: Request) -> bool:
    if not settings.WEBHOOK_SECRET:
        return True  # not configured -> mechanism inactive
    header = request.headers.get("x-jr-webhook-secret", "")
    if header and hmac.compare_digest(header, settings.WEBHOOK_SECRET):
        return True
    token = request.query_params.get("token", "")
    return bool(token) and hmac.compare_digest(token, settings.WEBHOOK_SECRET)


def _absolute_url(request: Request) -> str:
    """The URL as the provider saw it (needed for Twilio's signature).

    Behind a TLS proxy the internal request URL is wrong for signing; set
    JR_PUBLIC_BASE_URL to the external origin in production.
    """
    path = request.url.path
    query = request.url.query
    if settings.PUBLIC_BASE_URL:
        base = settings.PUBLIC_BASE_URL
    else:
        base = f"{request.url.scheme}://{request.url.netloc}"
    return f"{base}{path}" + (f"?{query}" if query else "")


def twilio_signature_valid(request: Request, form_params: dict[str, str]) -> bool:
    """Verify X-Twilio-Signature per Twilio's documented algorithm.

    Signature = base64(HMAC-SHA1(auth_token, url + "".join(name+value for
    name, value in sorted(post_params)))). Sorting is Unix-style
    case-sensitive, which is exactly Python's default string sort.
    """
    signature = request.headers.get("x-twilio-signature", "")
    if not signature or not settings.TWILIO_TOKEN:
        return False
    payload = _absolute_url(request) + "".join(
        f"{k}{form_params[k]}" for k in sorted(form_params)
    )
    expected = base64.b64encode(
        hmac.new(
            settings.TWILIO_TOKEN.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, signature)


def authorize_webhook(
    request: Request, provider: str, form_params: dict[str, str] | None = None
) -> tuple[bool, str]:
    """Return (allowed, reason). Every configured mechanism must pass."""
    if not _cidr_allowed(_client_ip(request)):
        return False, "source ip not in JR_WEBHOOK_CIDRS allowlist"
    if not _secret_presented(request):
        return False, "missing or wrong webhook secret"
    if provider == "twilio" and settings.TWILIO_TOKEN:
        # Twilio signs its webhooks; when we hold the auth token we insist on
        # a valid signature (unless a shared secret already authenticated the
        # request, which some deployments prefer for uniformity).
        if not settings.WEBHOOK_SECRET:
            if not twilio_signature_valid(request, form_params or {}):
                return False, "invalid X-Twilio-Signature"
    return True, ""
