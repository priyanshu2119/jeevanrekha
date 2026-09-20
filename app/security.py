"""Authentication and session handling.

Deliberately boring: PBKDF2-HMAC-SHA256 (stdlib, no native deps) with a
per-user salt and a high iteration count, plus a signed httpOnly session
cookie. Roles: admin (national/state), operator (dispatch desk), asha
(scoped to one region).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import wraps
from typing import Callable

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Role, User

_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="jr-session-v1")

SESSION_MAX_AGE = 12 * 3600  # a shift, not a month


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), settings.PBKDF2_ITERATIONS
    )
    return dk.hex(), salt


def verify_password(password: str, user: User) -> bool:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), user.salt.encode(), settings.PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(dk.hex(), user.password_hash)


def make_session_token(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session_token(token: str) -> int | None:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return int(data["uid"])
    except (BadSignature, KeyError, ValueError):
        return None


def current_user(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    uid = read_session_token(token)
    if uid is None:
        return None
    user = db.get(User, uid)
    if user and user.is_active:
        return user
    return None


def require_roles(*roles: Role) -> Callable:
    """Decorator for route handlers: kwargs must include db + user."""
    def deco(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = kwargs.get("user")
            if user is None or user.role not in {r.value for r in roles}:
                return None  # caller translates to 403/redirect
            return fn(*args, **kwargs)
        return wrapper
    return deco
