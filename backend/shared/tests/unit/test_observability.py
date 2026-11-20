"""Unit-тесты shared.observability: контракт корреляции и структурного лога."""
from __future__ import annotations

import json
import logging

import pytest

from shared.observability import (
    JsonFormatter,
    RequestIdFilter,
    bind_request_id,
    configure_logging,
    get_request_id,
    new_request_id,
    request_id_var,
    reset_request_id,
    set_request_id,
)


def test_new_request_id_is_hex32_and_unique() -> None:
    a, b = new_request_id(), new_request_id()
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a)
    assert a != b


def test_default_request_id_is_empty() -> None:
    assert request_id_var.get() == ""
    assert get_request_id() == ""


def test_set_and_reset_request_id() -> None:
    token = set_request_id("abc123")
    assert get_request_id() == "abc123"
    reset_request_id(token)
    assert get_request_id() == ""


def test_bind_request_id_generates_when_none_and_resets() -> None:
    assert get_request_id() == ""
    with bind_request_id() as rid:
        assert rid == get_request_id()
        assert len(rid) == 32
    assert get_request_id() == ""


def test_bind_request_id_uses_given_value() -> None:
    with bind_request_id("fixed-id") as rid:
        assert rid == "fixed-id"
        assert get_request_id() == "fixed-id"
    assert get_request_id() == ""


def test_request_id_filter_injects_dash_when_unset() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    assert RequestIdFilter().filter(record) is True
    assert vars(record)["request_id"] == "-"


def test_request_id_filter_injects_current_value() -> None:
    token = set_request_id("deadbeef")
    try:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
        RequestIdFilter().filter(record)
        assert vars(record)["request_id"] == "deadbeef"
    finally:
        reset_request_id(token)


def test_json_formatter_emits_all_fields() -> None:
    fmt = JsonFormatter(service_name="auth_api")
    record = logging.LogRecord("my.logger", logging.WARNING, __file__, 1, "hello", None, None)
    vars(record)["request_id"] = "rid-1"
    line = fmt.format(record)
    obj = json.loads(line)
    assert obj["level"] == "WARNING"
    assert obj["service"] == "auth_api"
    assert obj["logger"] == "my.logger"
    assert obj["request_id"] == "rid-1"
    assert obj["message"] == "hello"
    assert "ts" in obj


def test_json_formatter_includes_extras() -> None:
    fmt = JsonFormatter(service_name="svc")
    record = logging.LogRecord("l", logging.INFO, __file__, 1, "m", None, None)
    vars(record)["request_id"] = "-"
    vars(record)["route"] = "/api/v1/users"
    vars(record)["status"] = 200
    obj = json.loads(fmt.format(record))
    assert obj["route"] == "/api/v1/users"
    assert obj["status"] == 200


def test_configure_logging_honors_log_level_and_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_FORMAT", "json")
    configure_logging("test_svc")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
    assert any(isinstance(f, RequestIdFilter) for h in root.handlers for f in h.filters)
    assert logging.getLogger("tornado.access").level == logging.WARNING


def test_configure_logging_text_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("LOG_FORMAT", "text")
    configure_logging("test_svc")
    root = logging.getLogger()
    assert root.level == logging.INFO  # default
    assert all(not isinstance(h.formatter, JsonFormatter) for h in root.handlers)
