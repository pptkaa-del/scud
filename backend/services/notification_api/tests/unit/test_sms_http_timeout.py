import pytest

from app.main import (
    DEFAULT_MAX_SEND_TIMEOUT_SECONDS,
    DEFAULT_SMS_GATEWAY_TIMEOUT_SECONDS,
    DEFAULT_SMS_MAX_QUEUE_WAIT_SECONDS,
    MAX_POLL_TIMEOUT_SECONDS,
    MAX_SEND_TIMEOUT_ENV,
    SMS_GATEWAY_TIMEOUT_ENV,
    SMS_MAX_QUEUE_WAIT_ENV,
    build_max_poll_http,
    build_max_send_http,
    build_sms_http,
    read_sms_max_queue_wait,
)


def test_default_timeout_is_25(monkeypatch):
    monkeypatch.delenv(SMS_GATEWAY_TIMEOUT_ENV, raising=False)
    http = build_sms_http()
    assert DEFAULT_SMS_GATEWAY_TIMEOUT_SECONDS == 25.0
    assert http._request_timeout == 25.0


def test_timeout_from_env(monkeypatch):
    monkeypatch.setenv(SMS_GATEWAY_TIMEOUT_ENV, "42.5")
    assert build_sms_http()._request_timeout == 42.5


def test_sms_is_never_retried_and_url_is_redacted(monkeypatch):
    monkeypatch.delenv(SMS_GATEWAY_TIMEOUT_ENV, raising=False)
    http = build_sms_http()
    assert http._max_attempts == 1
    assert http._redact_url is True


def test_invalid_timeout_fails_fast(monkeypatch):
    monkeypatch.setenv(SMS_GATEWAY_TIMEOUT_ENV, "abc")
    with pytest.raises(ValueError):
        build_sms_http()


def test_queue_wait_default_and_env(monkeypatch):
    monkeypatch.delenv(SMS_MAX_QUEUE_WAIT_ENV, raising=False)
    assert DEFAULT_SMS_MAX_QUEUE_WAIT_SECONDS == 10.0
    assert read_sms_max_queue_wait() == 10.0
    monkeypatch.setenv(SMS_MAX_QUEUE_WAIT_ENV, "3.5")
    assert read_sms_max_queue_wait() == 3.5


def test_max_send_timeout_default_and_env(monkeypatch):
    monkeypatch.delenv(MAX_SEND_TIMEOUT_ENV, raising=False)
    assert DEFAULT_MAX_SEND_TIMEOUT_SECONDS == 8.0
    assert build_max_send_http()._request_timeout == 8.0
    monkeypatch.setenv(MAX_SEND_TIMEOUT_ENV, "6")
    assert build_max_send_http()._request_timeout == 6.0


def test_max_polling_keeps_long_timeout():
    assert MAX_POLL_TIMEOUT_SECONDS == 35.0
    assert build_max_poll_http()._request_timeout == 35.0


def test_timeout_budget_invariant():
    total = (
        DEFAULT_SMS_MAX_QUEUE_WAIT_SECONDS
        + DEFAULT_SMS_GATEWAY_TIMEOUT_SECONDS
        + DEFAULT_MAX_SEND_TIMEOUT_SECONDS
    )
    assert total == 43.0
    assert total < 45.0
