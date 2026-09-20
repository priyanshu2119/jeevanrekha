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

import httpx

from ..config import settings

log = logging.getLogger("jr.telephony")


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

    Uses the Exotel v1 REST API: a "connect" call dials the responder and
    plays a TTS message; DTMF input from the responder (1 = confirmed) is
    delivered to our webhook, which calls dispatch.confirm_from_provider().
    Requires JR_EXOTEL_SID / JR_EXOTEL_TOKEN in the environment.
    """

    name = "exotel"

    def __init__(self) -> None:
        self.sid = settings.EXOTEL_SID
        self.token = settings.EXOTEL_TOKEN
        self.subdomain = settings.EXOTEL_SUBDOMAIN
        self.base = f"https://{self.subdomain}.exotel.com/v1/Accounts/{self.sid}"

    def place_dispatch_alert(self, *, to_phone: str, message: str, **kwargs) -> ProviderResult:
        if not (self.sid and self.token):
            return ProviderResult(ok=False, detail="exotel credentials not configured")
        try:
            resp = httpx.post(
                f"{self.base}/Calls/connect",
                auth=(self.sid, self.token),
                data={
                    "From": settings.TWILIO_FROM or "08012345678",
                    "To": to_phone,
                    "CallerId": settings.TWILIO_FROM or "08012345678",
                    "Url": message,  # TTS XML/TwiML served by our webhook app
                },
                timeout=15.0,
            )
            ok = resp.status_code < 300
            return ProviderResult(
                ok=ok,
                detail=f"exotel http {resp.status_code}",
                provider_message_id=(resp.json().get("CallSid") if ok else None),
            )
        except httpx.HTTPError as exc:  # network failure must never crash dispatch
            log.warning("exotel call failed: %s", exc)
            return ProviderResult(ok=False, detail=f"exotel error: {exc}")

    def send_sms(self, to_phone: str, text: str) -> ProviderResult:
        if not (self.sid and self.token):
            return ProviderResult(ok=False, detail="exotel credentials not configured")
        try:
            resp = httpx.post(
                f"{self.base}/Sms/send",
                auth=(self.sid, self.token),
                data={"From": settings.TWILIO_FROM or "08012345678", "To": to_phone, "Body": text},
                timeout=15.0,
            )
            return ProviderResult(ok=resp.status_code < 300, detail=f"exotel http {resp.status_code}")
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, detail=f"exotel error: {exc}")


class TwilioProvider(TelephonyProvider):
    """Production adapter for Twilio Programmable Voice (where available)."""

    name = "twilio"

    def __init__(self) -> None:
        self.sid = settings.TWILIO_SID
        self.token = settings.TWILIO_TOKEN
        self.from_number = settings.TWILIO_FROM
        self.base = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}"

    def place_dispatch_alert(self, *, to_phone: str, message: str, **kwargs) -> ProviderResult:
        if not (self.sid and self.token and self.from_number):
            return ProviderResult(ok=False, detail="twilio credentials not configured")
        try:
            resp = httpx.post(
                f"{self.base}/Calls.json",
                auth=(self.sid, self.token),
                data={"To": to_phone, "From": self.from_number, "Twiml": message},
                timeout=15.0,
            )
            ok = resp.status_code < 300
            body = resp.json() if ok else {}
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
