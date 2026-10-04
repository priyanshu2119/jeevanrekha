"""CSRF protection for cookie-authenticated HTML forms.

Pattern: double-submit cookie. A random token lives in the ``jr_csrf`` cookie
(SameSite=Lax, HttpOnly) and is rendered into every staff form as a hidden
``csrf_token`` input. On POST the two must match. A cross-site attacker can
neither read the cookie (same-origin policy) nor make the browser send it on a
cross-site POST (SameSite=Lax), so both halves cannot be forged together.

The JSON APIs (flow, dispatch desk) are not form-encoded and are covered by
SameSite=Lax on the session cookie; this module guards the classic HTML form
endpoints: login and the admin region/contact configuration POSTs.
"""
from __future__ import annotations

import hmac
import secrets

from fastapi import Form, HTTPException, Request
from fastapi.responses import Response

from ..config import settings

CSRF_FORM_FIELD = "csrf_token"
_MAX_AGE = 12 * 3600  # same lifetime as a staff shift session


def csrf_token_for(request: Request) -> str:
    """Return the caller's CSRF token: the existing double-submit cookie
    value, or a fresh random token when the cookie is not there yet.

    Reusing the cookie value keeps already-open staff tabs working; the
    handler must call ``set_csrf_cookie`` when the cookie was absent.
    """
    return request.cookies.get(settings.CSRF_COOKIE) or secrets.token_hex(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.CSRF_COOKIE,
        token,
        max_age=_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
    )


def issue_csrf(request: Request, response: Response) -> str:
    """Convenience for handlers that build the response first: ensure the
    cookie exists and return the token in use."""
    token = csrf_token_for(request)
    if settings.CSRF_COOKIE not in request.cookies:
        set_csrf_cookie(response, token)
    return token


async def require_csrf(request: Request, csrf_token: str = Form("")) -> None:
    """FastAPI dependency: reject form POSTs whose token does not match the
    double-submit cookie."""
    if not settings.CSRF_ENABLED:
        return
    cookie = request.cookies.get(settings.CSRF_COOKIE, "")
    if not cookie or not csrf_token or not hmac.compare_digest(cookie, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
