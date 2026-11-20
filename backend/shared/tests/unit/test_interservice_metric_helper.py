"""Unit-тесты хелперов телеметрии межсервиса в retry_client:
_extract_target (host из URL, низкая кардинальность) и
record_interservice_metric (never-raises, no-op без prometheus_client)."""
from __future__ import annotations

from shared.http.retry_client import _extract_target, record_interservice_metric


def test_extract_target_docker_host() -> None:
    assert _extract_target("http://acm:8004/api/v1/check") == "acm"


def test_extract_target_with_port_and_path() -> None:
    assert _extract_target("http://user_api:8000/api/v1/internal/auth-check") == "user_api"


def test_extract_target_malformed_url_returns_unknown() -> None:
    assert _extract_target("not a url") == "unknown"


def test_record_interservice_metric_never_raises() -> None:
    # Невалидные/полные метки — не должно кидать ни при наличии, ни при
    # отсутствии prometheus_client.
    record_interservice_metric("acm", "GET", "success")
    record_interservice_metric("user_api", "POST", "exhausted")
