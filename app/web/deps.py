"""Shared web-layer plumbing: templates, auth dependencies, rendering helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..engine import i18n
from ..models import User
from ..security import current_user

templates: Jinja2Templates | None = None

IST = timezone(timedelta(hours=5, minutes=30))


def init_templates(directory: Path) -> None:
    global templates
    templates = Jinja2Templates(directory=str(directory))
    templates.env.globals["settings"] = settings
    templates.env.globals["language_names"] = i18n.LANGUAGE_NAMES
    templates.env.filters["ist"] = _ist_filter
    templates.env.filters["mask_phone"] = mask_phone


def _ist_filter(dt: datetime | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime(fmt)


def mask_phone(phone: str | None) -> str:
    """DPDP-minimal display: staff see the last four digits only unless the
    record belongs to their own region's follow-up queue."""
    if not phone:
        return "—"
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) <= 4:
        return "•" * len(digits)
    return "•" * (len(digits) - 4) + digits[-4:]


def get_templates() -> Jinja2Templates:
    assert templates is not None, "templates not initialised"
    return templates


def request_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(settings.SESSION_COOKIE)
    return current_user(db, token)
