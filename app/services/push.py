"""FCM push delivery (HTTP v1 API), optional by configuration.

Why push and not just the SSE feed: Android Doze/App-Standby kills
background sockets, and Google's own guidance for real-time alerting apps
is high-priority FCM -- the only sanctioned way to wake a killed device.
Messages are DATA-ONLY (no "notification" block) so the app's
onMessageReceived always runs and can raise a full-screen emergency alert
from the lock screen instead of the system tray doing it for us.

Deliberately lightweight: signs the service-account JWT with google-auth
and posts to fcm.googleapis.com via httpx -- no firebase-admin (which
drags in grpc/firestore/storage we would never use).

Delivery is best-effort and MUST NEVER break dispatch: every entry point
swallows and logs its own failures, and with JR_FCM_SERVICE_ACCOUNT unset
(the default, including tests) everything is a silent no-op.
"""
from __future__ import annotations

import json
import logging
import threading

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import CallSession, DeviceToken, Role, User

log = logging.getLogger("jr.push")

_FCM_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

_lock = threading.Lock()
_credentials = None
_project_id: str = ""


def enabled() -> bool:
    return bool(settings.FCM_SERVICE_ACCOUNT)


def _load_credentials():
    """Lazily load + cache the service-account credentials."""
    global _credentials, _project_id
    with _lock:
        if _credentials is None:
            from google.oauth2 import service_account  # lazy: optional dep

            with open(settings.FCM_SERVICE_ACCOUNT, encoding="utf-8") as fh:
                info = json.load(fh)
            _project_id = settings.FCM_PROJECT_ID or info.get("project_id", "")
            _credentials = service_account.Credentials.from_service_account_info(
                info, scopes=[_SCOPE]
            )
        return _credentials, _project_id


def _access_token() -> str | None:
    try:
        creds, _ = _load_credentials()
        if creds.expired or not creds.token:
            import google.auth.transport.requests

            creds.refresh(google.auth.transport.requests.Request())
        return creds.token
    except Exception:  # noqa: BLE001 - push must never raise into dispatch
        log.exception("FCM credential refresh failed; push disabled for now")
        return None


def send(device_token: str, data: dict, *, high_priority: bool) -> bool:
    """Deliver one data-only message to one device."""
    access = _access_token()
    if not access:
        return False
    _, project = _load_credentials()
    body = {
        "message": {
            "token": device_token,
            # FCM data values must all be strings.
            "data": {k: str(v) for k, v in data.items()},
            "android": {
                "priority": "high" if high_priority else "normal",
                # Keep undeliverable messages for a day: a phone that was off
                # during an emergency should still show the case on reconnect.
                "ttl": "86400s",
            },
        }
    }
    try:
        resp = httpx.post(
            _FCM_URL.format(project=project),
            headers={"Authorization": f"Bearer {access}"},
            json=body,
            timeout=5.0,  # short: push runs inline in dispatch paths
        )
        if resp.status_code >= 300:
            log.warning("FCM send failed http %s: %s",
                        resp.status_code, resp.text[:200])
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("FCM send error: %s", exc)
        return False


def notify_users(db: Session, user_ids: list[int], data: dict,
                 *, high_priority: bool = False) -> int:
    """Fan a message out to every active device of the given users."""
    if not enabled() or not user_ids:
        return 0
    rows = db.scalars(
        select(DeviceToken).where(
            DeviceToken.user_id.in_(user_ids),
            DeviceToken.is_active.is_(True),
        )
    ).all()
    sent = 0
    for row in rows:
        if send(row.token, data, high_priority=high_priority):
            sent += 1
    return sent


def targets_for_case(db: Session, session: CallSession | None) -> list[int]:
    """Who should hear about this case: operators + admins always; ASHA
    workers only for their own region (same scoping as the staff views --
    one region's emergencies must never leak into another's app)."""
    ids = list(db.scalars(
        select(User.id).where(
            User.is_active.is_(True),
            User.role.in_([Role.operator.value, Role.admin.value]),
        )
    ).all())
    if session is not None and session.region_id is not None:
        ids += list(db.scalars(
            select(User.id).where(
                User.is_active.is_(True),
                User.role == Role.asha.value,
                User.region_id == session.region_id,
            )
        ).all())
    return ids


def notify_case_event(db: Session, session: CallSession | None, kind: str,
                      *, high_priority: bool, **extra) -> None:
    """Best-effort fan-out of one dispatch event to staff devices.

    Called inline from dispatch.py; never raises, never blocks longer than
    the short HTTP timeout per device. At pilot scale (a handful of devices
    per region) inline delivery is fine; move to a queue if the device
    count grows.
    """
    try:
        if not enabled():
            return
        data = {
            "type": kind,
            "call_ref": session.ref_code if session else "",
            "region": session.region.label if (session and session.region) else "",
            **extra,
        }
        sent = notify_users(db, targets_for_case(db, session), data,
                            high_priority=high_priority)
        if sent:
            log.info("push %s for %s -> %s device(s)", kind,
                     session.ref_code if session else "?", sent)
    except Exception:  # noqa: BLE001 - push must never break dispatch
        log.exception("push notify failed (kind=%s)", kind)
