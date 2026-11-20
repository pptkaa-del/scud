from app.main import (
    DEFAULT_OTP_DELIVERY_TIMEOUT_SECONDS,
    OTP_DELIVERY_TIMEOUT_ENV,
    build_notification_http,
)


def test_default_timeout_is_45(monkeypatch):
    monkeypatch.delenv(OTP_DELIVERY_TIMEOUT_ENV, raising=False)
    assert DEFAULT_OTP_DELIVERY_TIMEOUT_SECONDS == 45.0
    assert build_notification_http()._request_timeout == 45.0


def test_timeout_from_env(monkeypatch):
    monkeypatch.setenv(OTP_DELIVERY_TIMEOUT_ENV, "60")
    assert build_notification_http()._request_timeout == 60.0
