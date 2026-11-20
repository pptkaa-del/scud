"""Unit-тесты инкремента scud_interservice_requests_total из RetryHTTPClient.

Проверяем, что симулированный 5xx-сценарий двигает счётчик с правильными
outcome: GET с двумя 5xx-фейлами и успехом на 3-й попытке → RETRY×2 + SUCCESS×1;
GET, исчерпавший все попытки → RETRY×(n-1) + EXHAUSTED×1; POST 5xx без opt-in →
ERROR×1 (не ретраится)."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, patch

from tornado.httpclient import HTTPError, HTTPRequest, HTTPResponse

from shared.enums import InterserviceOutcome
from shared.http import retry_client
from shared.http.retry_client import RetryHTTPClient
from shared.metrics import SERVICE_NAME as _METRICS_SERVICE_NAME, interservice_requests_total


def _ok_response(method: str) -> HTTPResponse:
    return HTTPResponse(HTTPRequest(url="http://svc/x", method=method), 200)


def _server_error() -> HTTPError:
    return HTTPError(503, message="Service Unavailable")


class _FailNTimes:
    def __init__(self, exc_factory: Callable[[], BaseException], n_fail: int, ok_method: str) -> None:
        self._exc_factory = exc_factory
        self._n_fail = n_fail
        self._ok_method = ok_method
        self.calls = 0

    def __call__(self, req: HTTPRequest, raise_error: bool = True) -> HTTPResponse:
        self.calls += 1
        if self.calls <= self._n_fail:
            raise self._exc_factory()
        return _ok_response(self._ok_method)


def _counter_value(target: str, method: str, outcome: str) -> float:
    c = interservice_requests_total.labels(
        service=_METRICS_SERVICE_NAME,
        target=target, method=method, outcome=outcome,
    )
    return float(c._value.get())


def _run(client: RetryHTTPClient, fake: _FailNTimes, *, method: str,
         idempotent: bool = False) -> None:
    async def scenario() -> None:
        with patch("shared.http.retry_client.AsyncHTTPClient") as cls:
            cls.return_value.fetch = AsyncMock(side_effect=fake)
            await client.fetch("http://acm:8004/x", method=method, idempotent=idempotent)
    asyncio.run(scenario())


def test_get_retry_then_success_moves_counter() -> None:
    assert retry_client._METRICS_AVAILABLE, "prometheus_client must be present in shared_tests image"
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    before_retry = _counter_value("acm", "GET", InterserviceOutcome.RETRY.value)
    before_success = _counter_value("acm", "GET", InterserviceOutcome.SUCCESS.value)
    fake = _FailNTimes(_server_error, n_fail=2, ok_method="GET")
    _run(client, fake, method="GET")
    assert _counter_value("acm", "GET", InterserviceOutcome.RETRY.value) == before_retry + 2
    assert _counter_value("acm", "GET", InterserviceOutcome.SUCCESS.value) == before_success + 1


def test_get_exhausted_moves_counter() -> None:
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    before_retry = _counter_value("acm", "GET", InterserviceOutcome.RETRY.value)
    before_exhausted = _counter_value("acm", "GET", InterserviceOutcome.EXHAUSTED.value)
    fake = _FailNTimes(_server_error, n_fail=99, ok_method="GET")
    raised = False
    try:
        _run(client, fake, method="GET")
    except HTTPError:
        raised = True
    assert raised
    # max_attempts=3 → 2 ретраябельных перехода + 1 финальный EXHAUSTED.
    assert _counter_value("acm", "GET", InterserviceOutcome.RETRY.value) == before_retry + 2
    assert _counter_value("acm", "GET", InterserviceOutcome.EXHAUSTED.value) == before_exhausted + 1


def test_post_5xx_no_optin_moves_error_counter() -> None:
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    before = _counter_value("acm", "POST", InterserviceOutcome.ERROR.value)
    fake = _FailNTimes(_server_error, n_fail=99, ok_method="POST")
    raised = False
    try:
        _run(client, fake, method="POST")
    except HTTPError:
        raised = True
    assert raised
    assert _counter_value("acm", "POST", InterserviceOutcome.ERROR.value) == before + 1
