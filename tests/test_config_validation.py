"""Production configuration validation: the app must refuse to boot in a
configuration that would be unsafe in front of real callers."""
import pytest

from app.config import settings


def _production(monkeypatch, **overrides):
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "SECRET_KEY", "a-real-random-secret")
    monkeypatch.setattr(settings, "TELEPHONY_PROVIDER", "simulated")
    monkeypatch.setattr(settings, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOWED_CIDRS", "")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://api.jr.example")
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+psycopg://jr:jr@db/jr")
    for k, v in overrides.items():
        monkeypatch.setattr(settings, k, v)


class TestValidateRuntime:
    def test_development_is_permissive(self, monkeypatch):
        monkeypatch.setattr(settings, "ENV", "development")
        settings.validate_runtime()  # must not raise with stock dev defaults

    def test_production_refuses_default_secret_key(self, monkeypatch):
        _production(monkeypatch, SECRET_KEY="dev-only-insecure-key-change-me")
        with pytest.raises(RuntimeError, match="JR_SECRET_KEY"):
            settings.validate_runtime()

    def test_production_refuses_open_webhooks_with_real_provider(self, monkeypatch):
        _production(monkeypatch, TELEPHONY_PROVIDER="exotel")
        with pytest.raises(RuntimeError, match="webhook"):
            settings.validate_runtime()

    def test_production_accepts_secret_or_cidr(self, monkeypatch):
        _production(monkeypatch, TELEPHONY_PROVIDER="exotel",
                    WEBHOOK_SECRET="tok")
        settings.validate_runtime()
        _production(monkeypatch, TELEPHONY_PROVIDER="twilio",
                    WEBHOOK_ALLOWED_CIDRS="13.126.0.0/16")
        settings.validate_runtime()

    def test_production_simulated_provider_needs_no_webhook_auth(self, monkeypatch):
        _production(monkeypatch, TELEPHONY_PROVIDER="simulated")
        settings.validate_runtime()  # dispatch desk is login-gated instead

    def test_production_real_provider_requires_public_base_url(self, monkeypatch):
        # ExoML/TwiML action URLs and outbound call flows must be absolute
        # and publicly reachable -- a real provider cannot call "localhost".
        _production(monkeypatch, TELEPHONY_PROVIDER="exotel",
                    WEBHOOK_SECRET="tok", PUBLIC_BASE_URL="")
        with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
            settings.validate_runtime()
