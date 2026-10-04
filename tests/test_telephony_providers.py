"""Provider adapter HTTP contracts (mocked transport).

The adapters are the only code that talks to a vendor; these tests pin the
exact request shape verified against Exotel's published API docs and their
official goexoml/ExotelMCP examples:

* Calls/connect.json "number to call flow" pattern: From = the number being
  CALLED (the responder), CallerId = our ExoPhone, Url = ExoML endpoint,
  StatusCallback = terminal-status endpoint. (The pre-Phase-2 code passed the
  TTS message text as the Url and swapped From/To -- it could never work.)
* Sms/send.json with From/To/Body plus DLT template/entity when configured
  (TRAI: Indian carriers drop SMS without a registered template).
"""
import httpx
import pytest

from app.config import settings
from app.services.telephony import (
    ExotelProvider,
    TwilioProvider,
    get_provider,
    webhook_url,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture()
def exotel_env(monkeypatch):
    monkeypatch.setattr(settings, "EXOTEL_SID", "jr-sid")
    monkeypatch.setattr(settings, "EXOTEL_TOKEN", "jr-token")
    monkeypatch.setattr(settings, "EXOTEL_FROM", "08011122233")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://api.jr.example")
    monkeypatch.setattr(settings, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(settings, "EXOTEL_DLT_TEMPLATE_ID", "")
    monkeypatch.setattr(settings, "EXOTEL_DLT_ENTITY_ID", "")


@pytest.fixture()
def captured(monkeypatch):
    calls = {}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls.update(kwargs)
        return FakeResponse(200, {"Call": {"Sid": "CALL123"}, "sid": "CALL123"})

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


class TestWebhookUrl:
    def test_absolute_with_params(self, exotel_env):
        url = webhook_url("/webhooks/exotel/responder-alert", event_id=7)
        assert url == "https://api.jr.example/webhooks/exotel/responder-alert?event_id=7"

    def test_secret_rides_along_as_token(self, exotel_env, monkeypatch):
        monkeypatch.setattr(settings, "WEBHOOK_SECRET", "tok123")
        url = webhook_url("/webhooks/exotel/call-status", event_id=7)
        assert "event_id=7" in url and "token=tok123" in url


class TestExotelProvider:
    def test_connect_uses_flow_pattern_not_message_as_url(self, exotel_env, captured):
        result = ExotelProvider().place_dispatch_alert(
            to_phone="9000000001", to_name="ASHA One", kind="asha",
            message="speak this text", call_ref="JR-1", case_id=1,
            track="backup", event_id=42,
        )
        assert result.ok
        assert result.provider_message_id == "CALL123"
        assert captured["url"].endswith("/v1/Accounts/jr-sid/Calls/connect.json")
        data = captured["data"]
        assert data["From"] == "9000000001"        # responder is the CALLEE
        assert data["CallerId"] == "08011122233"   # our ExoPhone
        assert data["Url"].startswith("https://api.jr.example/webhooks/exotel/responder-alert")
        assert "event_id=42" in data["Url"]
        assert "speak this text" not in data["Url"]  # the old, fatal bug
        assert "call-status" in data["StatusCallback"]
        assert "event_id=42" in data["StatusCallback"]
        assert captured["auth"] == ("jr-sid", "jr-token")

    def test_missing_config_fails_closed(self, exotel_env, monkeypatch):
        monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
        result = ExotelProvider().place_dispatch_alert(
            to_phone="9000000001", to_name="x", kind="asha", message="m",
            call_ref="JR-1", case_id=1, track="backup", event_id=1)
        assert not result.ok
        assert "JR_PUBLIC_BASE_URL" in result.detail

        monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://api.jr.example")
        monkeypatch.setattr(settings, "EXOTEL_FROM", "")
        result = ExotelProvider().place_dispatch_alert(
            to_phone="9000000001", to_name="x", kind="asha", message="m",
            call_ref="JR-1", case_id=1, track="backup", event_id=1)
        assert not result.ok and "JR_EXOTEL_FROM" in result.detail

    def test_http_error_never_raises(self, exotel_env, monkeypatch):
        def boom(*a, **k):
            raise httpx.ConnectError("network down")
        monkeypatch.setattr(httpx, "post", boom)
        result = ExotelProvider().place_dispatch_alert(
            to_phone="9000000001", to_name="x", kind="asha", message="m",
            call_ref="JR-1", case_id=1, track="backup", event_id=1)
        assert not result.ok and "network down" in result.detail

    def test_sms_carries_dlt_fields_when_configured(self, exotel_env, captured, monkeypatch):
        monkeypatch.setattr(settings, "EXOTEL_DLT_TEMPLATE_ID", "1001")
        monkeypatch.setattr(settings, "EXOTEL_DLT_ENTITY_ID", "2002")
        result = ExotelProvider().send_sms("9000000001", "emergency text")
        assert result.ok
        assert captured["url"].endswith("/Sms/send.json")
        data = captured["data"]
        assert data["From"] == "08011122233"
        assert data["To"] == "9000000001"
        assert data["Body"] == "emergency text"
        assert data["DltTemplateId"] == "1001"
        assert data["DltEntityId"] == "2002"

    def test_sms_omits_dlt_when_unset(self, exotel_env, captured):
        ExotelProvider().send_sms("9000000001", "text")
        assert "DltTemplateId" not in captured["data"]


class TestTwilioProvider:
    @pytest.fixture()
    def twilio_env(self, monkeypatch):
        monkeypatch.setattr(settings, "TWILIO_SID", "AC123")
        monkeypatch.setattr(settings, "TWILIO_TOKEN", "tw-token")
        monkeypatch.setattr(settings, "TWILIO_FROM", "+15551234567")
        monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://api.jr.example")
        monkeypatch.setattr(settings, "WEBHOOK_SECRET", "")

    def test_call_uses_url_flow_not_inline_text(self, twilio_env, captured):
        result = TwilioProvider().place_dispatch_alert(
            to_phone="+919000000001", to_name="ASHA", kind="asha",
            message="speak this", call_ref="JR-2", case_id=2,
            track="backup", event_id=9)
        assert result.ok and result.provider_message_id == "CALL123"
        assert captured["url"].endswith("/Accounts/AC123/Calls.json")
        data = captured["data"]
        assert data["To"] == "+919000000001"
        assert data["From"] == "+15551234567"
        assert "twilio/responder-alert" in data["Url"] and "event_id=9" in data["Url"]
        assert "speak this" not in str(data)

    def test_provider_selection(self, monkeypatch):
        monkeypatch.setattr(settings, "TELEPHONY_PROVIDER", "exotel")
        assert get_provider().name == "exotel"
        monkeypatch.setattr(settings, "TELEPHONY_PROVIDER", "twilio")
        assert get_provider().name == "twilio"
        monkeypatch.setattr(settings, "TELEPHONY_PROVIDER", "simulated")
        assert get_provider().name == "simulated"
