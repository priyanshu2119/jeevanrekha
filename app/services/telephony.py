"""Telephony provider abstraction.

The call-flow state machine and the dispatch engine never talk to a vendor
directly -- they talk to this interface. That keeps the safety-critical logic
testable offline and swappable in production:

* ``SimulatedProvider`` (default): fully functional local harness. Outbound
  alerts land in a "dispatch desk" inbox where a human playing the role of
  the ambulance node / ASHA / PHC confirms or declines. This is what makes
  the parallel-routing and escalation behaviour end-to-end testable without
  a PSTN.
* ``ExotelProvider`` / ``TwilioProvider``: production adapters for the real
  voice path (India-first cloud telephony; DTMF webhooks drive the same
  state machine). They perform real HTTP calls when credentials are
  configured; outbound dispatch alerts are placed as voice calls carrying a
  TTS message, with an SMS fallback.

No LLM or speech recognition is involved anywhere: the interaction is DTMF
("press 1 for yes"), which works on every basic handset in India and is
deterministic by construction.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

import httpx

from ..config import settings

log = logging.getLogger("jr.telephony")


def webhook_url(path: str, **params) -> str:
    """Absolute URL to one of our own webhook endpoints.

    Providers fetch these URLs from their side, so they must be absolute and
    publicly reachable (JR_PUBLIC_BASE_URL). The shared webhook secret rides
    along as ?token= when configured -- Exotel dashboard/ExoML URLs cannot
    attach custom headers, and our webhook auth accepts either form.
    """
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    query = {k: str(v) for k, v in params.items() if v not in (None, "")}
    if settings.WEBHOOK_SECRET:
        query["token"] = settings.WEBHOOK_SECRET
    return f"{base}{path}" + (f"?{urlencode(query)}" if query else "")


@dataclass
class ProviderResult:
    ok: bool
    detail: str = ""
    provider_message_id: str | None = None


@dataclass
class OutboundAlert:
    """One simulated outbound contact attempt, shown on the dispatch desk."""
    id: int | None = None
    to_name: str = ""
    to_phone: str = ""
    kind: str = ""
    message: str = ""
    call_ref: str = ""
    case_id: int | None = None
    track: str = ""
    event_id: int | None = None
    created_at: datetime | None = None
    resolved: bool = False
    resolution: str = ""


class TelephonyProvider(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def place_dispatch_alert(
        self,
        *,
        to_phone: str,
        to_name: str,
        kind: str,
        message: str,
        call_ref: str,
        case_id: int,
        track: str,
        event_id: int | None = None,
    ) -> ProviderResult:
        """Initiate contact toward a responder (ambulance node or backup)."""

    @abc.abstractmethod
    def send_sms(self, to_phone: str, text: str) -> ProviderResult:
        """SMS fallback / supplementary alert."""


class SimulatedProvider(TelephonyProvider):
    """Local harness.

    Alerts are persisted through a callback supplied by the dispatch service
    (they live in the same SQLite database as everything else, in the
    ``sim_outbox`` table), so the dispatch desk survives restarts and tests
    can drive confirmations programmatically.
    """

    name = "simulated"

    def place_dispatch_alert(self, **kwargs) -> ProviderResult:
        # Persistence happens in dispatch.py (it owns the DB session); here we
        # only model the transport itself, which locally always "rings".
        log.info(
            "simulated outbound alert -> %s (%s) for call %s track %s",
            kwargs.get("to_phone"), kwargs.get("to_name"),
            kwargs.get("call_ref"), kwargs.get("track"),
        )
        return ProviderResult(ok=True, detail="simulated call placed; awaiting human confirmation on dispatch desk")

    def send_sms(self, to_phone: str, text: str) -> ProviderResult:
        log.info("simulated SMS -> %s: %s", to_phone, text[:80])
        return ProviderResult(ok=True, detail="simulated sms queued")


class ExotelProvider(TelephonyProvider):
    """Production adapter for Exotel (India-first CPaaS).

    Outbound dispatch alerts use the documented "connect a number to a call
    flow" pattern of ``POST /v1/Accounts/{sid}/Calls/connect.json``:

    * ``From``     -- the number being CALLED (the responder),
    * ``CallerId`` -- our ExoPhone (what the responder sees),
    * ``Url``      -- an endpoint of ours that serves ExoML; Exotel fetches
                      it when the responder answers and executes the verbs
                      (Say the alert, Gather "press 1 to confirm"),
    * ``StatusCallback`` -- Exotel posts the terminal call status (completed
                      / busy / no-answer / failed) so a responder who never
                      picks up escalates IMMEDIATELY instead of burning the
                      whole confirmation window.

    The responder's DTMF choice lands on /webhooks/responder-confirm with the
    exact event id, which calls dispatch.confirm()/decline().
    Requires JR_EXOTEL_SID / JR_EXOTEL_TOKEN / JR_EXOTEL_FROM /
    JR_PUBLIC_BASE_URL in the environment.
    """

    name = "exotel"

    def __init__(self) -> None:
        self.sid = settings.EXOTEL_SID
        self.token = settings.EXOTEL_TOKEN
        self.subdomain = settings.EXOTEL_SUBDOMAIN
        self.base = f"https://{self.subdomain}.exotel.com/v1/Accounts/{self.sid}"

    def _missing_config(self) -> list[str]:
        return [name for name, value in (
            ("JR_EXOTEL_SID", self.sid),
            ("JR_EXOTEL_TOKEN", self.token),
            ("JR_EXOTEL_FROM", settings.EXOTEL_FROM),
            ("JR_PUBLIC_BASE_URL", settings.PUBLIC_BASE_URL),
        ) if not value]

    def place_dispatch_alert(self, *, to_phone: str, message: str,
                             event_id: int | None = None, **kwargs) -> ProviderResult:
        missing = self._missing_config()
        if missing:
            return ProviderResult(ok=False,
                                  detail=f"exotel not configured: {', '.join(missing)}")
        flow_url = webhook_url("/webhooks/exotel/responder-alert", event_id=event_id)
        status_url = webhook_url("/webhooks/exotel/call-status", event_id=event_id)
        try:
            resp = httpx.post(
                f"{self.base}/Calls/connect.json",
                auth=(self.sid, self.token),
                data={
                    "From": to_phone,                    # the responder being called
                    "CallerId": settings.EXOTEL_FROM,    # our ExoPhone
                    "Url": flow_url,                     # ExoML: Say + Gather
                    "StatusCallback": status_url,
                },
                timeout=15.0,
            )
            ok = resp.status_code < 300
            call_sid = None
            if ok:
                try:
                    call_sid = (resp.json().get("Call") or {}).get("Sid")
                except ValueError:
                    pass
            else:
                log.warning("exotel Calls/connect failed http %s: %s",
                            resp.status_code, resp.text[:200])
            return ProviderResult(
                ok=ok,
                detail=f"exotel http {resp.status_code}",
                provider_message_id=call_sid,
            )
        except httpx.HTTPError as exc:  # network failure must never crash dispatch
            log.warning("exotel call failed: %s", exc)
            return ProviderResult(ok=False, detail=f"exotel error: {exc}")

    def send_sms(self, to_phone: str, text: str) -> ProviderResult:
        if not (self.sid and self.token):
            return ProviderResult(ok=False, detail="exotel credentials not configured")
        data: dict[str, str] = {
            "From": settings.EXOTEL_FROM or self.sid,
            "To": to_phone,
            "Body": text,
        }
        # TRAI DLT: Indian carriers drop SMS that do not reference a
        # registered content template / Principal Entity.
        if settings.EXOTEL_DLT_TEMPLATE_ID:
            data["DltTemplateId"] = settings.EXOTEL_DLT_TEMPLATE_ID
        if settings.EXOTEL_DLT_ENTITY_ID:
            data["DltEntityId"] = settings.EXOTEL_DLT_ENTITY_ID
        try:
            resp = httpx.post(
                f"{self.base}/Sms/send.json",
                auth=(self.sid, self.token),
                data=data,
                timeout=15.0,
            )
            return ProviderResult(ok=resp.status_code < 300,
                                  detail=f"exotel http {resp.status_code}")
        except httpx.HTTPError as exc:
            log.warning("exotel sms failed: %s", exc)
            return ProviderResult(ok=False, detail=f"exotel error: {exc}")


class TwilioProvider(TelephonyProvider):
    """Production adapter for Twilio Programmable Voice.

    NOTE: Twilio cannot serve domestic India-to-India voice (their own India
    guidelines mark domestic inbound/outbound as N/A), so for the target
    deployment Exotel is the provider; this adapter exists for deployments
    where Twilio does hold usable numbers. Outbound alerts fetch TwiML from
    our /webhooks/twilio/responder-alert endpoint (same Gather pattern).
    """

    name = "twilio"

    def __init__(self) -> None:
        self.sid = settings.TWILIO_SID
        self.token = settings.TWILIO_TOKEN
        self.from_number = settings.TWILIO_FROM
        self.base = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}"

    def place_dispatch_alert(self, *, to_phone: str, message: str,
                             event_id: int | None = None, **kwargs) -> ProviderResult:
        if not (self.sid and self.token and self.from_number and settings.PUBLIC_BASE_URL):
            return ProviderResult(
                ok=False,
                detail="twilio not configured: need JR_TWILIO_SID, JR_TWILIO_TOKEN, "
                       "JR_TWILIO_FROM and JR_PUBLIC_BASE_URL")
        try:
            resp = httpx.post(
                f"{self.base}/Calls.json",
                auth=(self.sid, self.token),
                data={
                    "To": to_phone,
                    "From": self.from_number,
                    "Url": webhook_url("/webhooks/twilio/responder-alert",
                                       event_id=event_id),
                    "StatusCallback": webhook_url("/webhooks/twilio/call-status",
                                                  event_id=event_id),
                },
                timeout=15.0,
            )
            ok = resp.status_code < 300
            body = {}
            if ok:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
            return ProviderResult(
                ok=ok,
                detail=f"twilio http {resp.status_code}",
                provider_message_id=body.get("sid"),
            )
        except httpx.HTTPError as exc:
            log.warning("twilio call failed: %s", exc)
            return ProviderResult(ok=False, detail=f"twilio error: {exc}")

    def send_sms(self, to_phone: str, text: str) -> ProviderResult:
        if not (self.sid and self.token and self.from_number):
            return ProviderResult(ok=False, detail="twilio credentials not configured")
        try:
            resp = httpx.post(
                f"{self.base}/Messages.json",
                auth=(self.sid, self.token),
                data={"To": to_phone, "From": self.from_number, "Body": text},
                timeout=15.0,
            )
            return ProviderResult(ok=resp.status_code < 300, detail=f"twilio http {resp.status_code}")
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, detail=f"twilio error: {exc}")


def get_provider() -> TelephonyProvider:
    kind = settings.TELEPHONY_PROVIDER.lower()
    if kind == "exotel":
        return ExotelProvider()
    if kind == "twilio":
        return TwilioProvider()
    return SimulatedProvider()
