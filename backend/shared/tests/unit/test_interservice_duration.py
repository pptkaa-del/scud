"""Unit-тесты scud_interservice_request_duration_seconds.

Проверяем:
a) _status_class: None→2xx, 404→4xx, 503→5xx, 599/TimeoutError/OSError→network_error.
b) record_interservice_duration: кладёт одно наблюдение, никогда не кидает.
c) fetch, ретраящийся дважды и затем завершающийся успехом → РОВНО ОДНО
   наблюдение на histogram (total elapsed, не per-retry); 4xx fast-fail →
   одно наблюдение с status_class="4xx".
d) fetch, исчерпавший все попытки на ConnectionRefusedError → РОВНО ОДНО
   наблюдение с status_class="network_error".
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, patch

from tornado.httpclient import HTTPError, HTTPRequest, HTTPResponse

from shared.enums import InterserviceStatusClass
from shared.http.retry_client import RetryHTTPClient, _method_label, _status_class
from shared.metrics import (
    SERVICE_NAME as _METRICS_SERVICE_NAME,
    interservice_request_duration_seconds,
    record_interservice_duration,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ok_response(method: str = "GET") -> HTTPResponse:
    return HTTPResponse(HTTPRequest(url="http://svc/x", method=method), 200)


def _histogram_count(target: str, method: str, status_class: str) -> float:
    """Возвращает _count накопленных наблюдений по label-набору.

    Использует публичный API collect() + итерацию по Sample'ам, чтобы не
    зависеть от внутренней структуры prometheus_client (версия 0.16.0 в
    Astra не имеет дочернего атрибута _count на labeled Histogram).
    """
    want = {
        "service": _METRICS_SERVICE_NAME,
        "target": target,
        "method": method.upper(),
        "status_class": status_class,
    }
    for mf in interservice_request_duration_seconds.collect():
        for sample in mf.samples:
            if sample.name.endswith("_count") and sample.labels == want:
                return float(sample.value)
    return 0.0


class _FailNTimes:
    """Кидает exc_factory() первые n_fail раз, затем отдаёт ok-ответ."""

    def __init__(
        self,
        exc_factory: Callable[[], BaseException],
        n_fail: int,
        ok_method: str = "GET",
    ) -> None:
        self._exc_factory = exc_factory
        self._n_fail = n_fail
        self._ok_method = ok_method
        self.calls = 0

    def __call__(self, req: HTTPRequest, raise_error: bool = True) -> HTTPResponse:
        self.calls += 1
        if self.calls <= self._n_fail:
            raise self._exc_factory()
        return _ok_response(self._ok_method)


# ---------------------------------------------------------------------------
# a) _status_class helper
# ---------------------------------------------------------------------------

def test_status_class_none_is_2xx() -> None:
    assert _status_class(None) == "2xx"


def test_status_class_404_is_4xx() -> None:
    assert _status_class(HTTPError(404, "Not Found")) == "4xx"


def test_status_class_400_is_4xx() -> None:
    assert _status_class(HTTPError(400, "Bad Request")) == "4xx"


def test_status_class_503_is_5xx() -> None:
    assert _status_class(HTTPError(503, "Service Unavailable")) == "5xx"


def test_status_class_500_is_5xx() -> None:
    assert _status_class(HTTPError(500, "Internal Server Error")) == "5xx"


def test_status_class_599_is_network_error() -> None:
    # 599 — Tornado-код для timeout/connection-failure
    assert _status_class(HTTPError(599, "Timeout")) == "network_error"


def test_status_class_timeout_error_is_network_error() -> None:
    assert _status_class(TimeoutError("timed out")) == "network_error"


def test_status_class_oserror_is_network_error() -> None:
    assert _status_class(OSError("connection reset")) == "network_error"


def test_status_class_connection_refused_is_network_error() -> None:
    assert _status_class(ConnectionRefusedError("refused")) == "network_error"


# ---------------------------------------------------------------------------
# _method_label helper
# ---------------------------------------------------------------------------

def test_method_label_standard_methods_uppercase() -> None:
    for method in ("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"):
        assert _method_label(method) == method
        assert _method_label(method.lower()) == method


def test_method_label_nonstandard_renders_OTHER() -> None:
    assert _method_label("PROPFIND") == "OTHER"
    assert _method_label("custom") == "OTHER"


def test_method_label_nonstandard_one_observation() -> None:
    """Non-standard method → histogram uses 'OTHER' label, not lowercase 'other'."""
    before = _histogram_count("svc_method", "OTHER", "2xx")
    record_interservice_duration(
        "svc_method", _method_label("PROPFIND"), InterserviceStatusClass.TWO_XX, 0.01
    )
    after = _histogram_count("svc_method", "OTHER", "2xx")
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# b) record_interservice_duration: наблюдает одно значение, никогда не кидает
# ---------------------------------------------------------------------------

def test_record_interservice_duration_observes_one_sample() -> None:
    before = _histogram_count("test_svc", "GET", "2xx")
    record_interservice_duration("test_svc", "GET", InterserviceStatusClass.TWO_XX, 0.01)
    after = _histogram_count("test_svc", "GET", "2xx")
    assert after - before == 1.0


def test_record_interservice_duration_never_raises_on_bad_value() -> None:
    # float("inf") и отрицательные секунды — prometheus_client их принимает,
    # но главное — функция не должна кидать ни при каком значении.
    record_interservice_duration("t", "GET", InterserviceStatusClass.TWO_XX, float("inf"))
    record_interservice_duration("t", "GET", InterserviceStatusClass.TWO_XX, -1.0)
    # Тип-неверное значение seconds гарантированно бросает в prometheus_client,
    # проверяем что except-ветка в record_interservice_duration работает.
    record_interservice_duration("t", "GET", InterserviceStatusClass.TWO_XX, "not_a_number")  # type: ignore[arg-type]


def test_record_interservice_duration_wrong_status_class_does_not_raise() -> None:
    # Enum-member — всегда правильный; этот тест оставлен для документации.
    record_interservice_duration("t", "GET", InterserviceStatusClass.NETWORK_ERROR, 0.5)


# ---------------------------------------------------------------------------
# c) fetch: ровно одно наблюдение на вызов (независимо от числа retry)
# ---------------------------------------------------------------------------

def test_fetch_retry_then_success_exactly_one_observation() -> None:
    """GET с 2 фейлами 503 + успех → histogram_count+=1 с status_class=2xx.

    Дополнительно: 5xx и network_error НЕ наблюдаются на retry-пути — проверяем
    дельты == 0, чтобы регрессия retry-path была поймана.
    """
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    fake = _FailNTimes(lambda: HTTPError(503, "Unavailable"), n_fail=2, ok_method="GET")

    before = _histogram_count("svc", "GET", "2xx")
    before_5xx = _histogram_count("svc", "GET", "5xx")
    before_net = _histogram_count("svc", "GET", "network_error")

    async def scenario() -> None:
        with patch("shared.http.retry_client.AsyncHTTPClient") as cls:
            cls.return_value.fetch = AsyncMock(side_effect=fake)
            await client.fetch("http://svc/x", method="GET")

    asyncio.run(scenario())

    after = _histogram_count("svc", "GET", "2xx")
    assert after - before == 1.0, (
        f"Expected exactly 1 observation after retry-then-success, "
        f"got {after - before}"
    )
    assert fake.calls == 3, "sanity: fake was called 3× (2 fail + 1 success)"
    assert _histogram_count("svc", "GET", "5xx") - before_5xx == 0.0, (
        "5xx must NOT be observed on retry path"
    )
    assert _histogram_count("svc", "GET", "network_error") - before_net == 0.0, (
        "network_error must NOT be observed on retry path"
    )


def test_fetch_4xx_fast_fail_one_observation_with_4xx_class() -> None:
    """4xx fast-fail → одно наблюдение status_class=4xx, исключение пробрасывается."""
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    fake = _FailNTimes(lambda: HTTPError(404, "Not Found"), n_fail=99, ok_method="GET")

    before = _histogram_count("svc", "GET", "4xx")

    raised = False

    async def scenario() -> None:
        nonlocal raised
        with patch("shared.http.retry_client.AsyncHTTPClient") as cls:
            cls.return_value.fetch = AsyncMock(side_effect=fake)
            try:
                await client.fetch("http://svc/x", method="GET")
            except HTTPError:
                raised = True

    asyncio.run(scenario())

    assert raised, "4xx must propagate"
    after = _histogram_count("svc", "GET", "4xx")
    assert after - before == 1.0, (
        f"Expected 1 observation with status_class=4xx, got {after - before}"
    )
    assert fake.calls == 1, "4xx is never retried"


def test_fetch_exhausted_one_observation_with_5xx_class() -> None:
    """GET, исчерпавший все 3 попытки на 503 → одно наблюдение status_class=5xx."""
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    fake = _FailNTimes(lambda: HTTPError(503, "Unavailable"), n_fail=99, ok_method="GET")

    before = _histogram_count("svc", "GET", "5xx")

    raised = False

    async def scenario() -> None:
        nonlocal raised
        with patch("shared.http.retry_client.AsyncHTTPClient") as cls:
            cls.return_value.fetch = AsyncMock(side_effect=fake)
            try:
                await client.fetch("http://svc/x", method="GET")
            except HTTPError:
                raised = True

    asyncio.run(scenario())

    assert raised, "exhausted must propagate"
    after = _histogram_count("svc", "GET", "5xx")
    assert after - before == 1.0, (
        f"Expected 1 observation with status_class=5xx, got {after - before}"
    )


def test_fetch_post_ambiguous_5xx_one_observation() -> None:
    """POST 5xx без opt-in → одно наблюдение status_class=5xx (не ретраится)."""
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    fake = _FailNTimes(lambda: HTTPError(503, "Unavailable"), n_fail=99, ok_method="POST")

    before = _histogram_count("svc", "POST", "5xx")

    raised = False

    async def scenario() -> None:
        nonlocal raised
        with patch("shared.http.retry_client.AsyncHTTPClient") as cls:
            cls.return_value.fetch = AsyncMock(side_effect=fake)
            try:
                await client.fetch("http://svc/x", method="POST")
            except HTTPError:
                raised = True

    asyncio.run(scenario())

    assert raised
    after = _histogram_count("svc", "POST", "5xx")
    assert after - before == 1.0
    assert fake.calls == 1, "POST 5xx without opt-in must not be retried"


def test_fetch_timeout_oserror_one_observation_network_error() -> None:
    """POST TimeoutError без opt-in → одно наблюдение status_class=network_error."""
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    fake = _FailNTimes(lambda: TimeoutError("timed out"), n_fail=99, ok_method="POST")

    before = _histogram_count("svc", "POST", "network_error")

    raised = False

    async def scenario() -> None:
        nonlocal raised
        with patch("shared.http.retry_client.AsyncHTTPClient") as cls:
            cls.return_value.fetch = AsyncMock(side_effect=fake)
            try:
                await client.fetch("http://svc/x", method="POST")
            except (TimeoutError, OSError):
                raised = True

    asyncio.run(scenario())

    assert raised
    after = _histogram_count("svc", "POST", "network_error")
    assert after - before == 1.0
    assert fake.calls == 1, "POST TimeoutError without opt-in must not be retried"


def test_fetch_exhausted_oserror_one_observation_network_error() -> None:
    """GET исчерпывает 3 попытки на ConnectionRefusedError → ровно одно наблюдение
    network_error, и underlying fake вызывается ровно 3 раза."""
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    fake = _FailNTimes(lambda: ConnectionRefusedError("refused"), n_fail=99, ok_method="GET")

    before_net = _histogram_count("svc", "GET", "network_error")

    raised = False

    async def scenario() -> None:
        nonlocal raised
        with patch("shared.http.retry_client.AsyncHTTPClient") as cls:
            cls.return_value.fetch = AsyncMock(side_effect=fake)
            try:
                await client.fetch("http://svc/x", method="GET")
            except (ConnectionRefusedError, OSError):
                raised = True

    asyncio.run(scenario())

    assert raised, "exhausted ConnectionRefusedError must propagate"
    assert fake.calls == 3, "must have attempted exactly max_attempts=3 times"
    assert _histogram_count("svc", "GET", "network_error") - before_net == 1.0, (
        "Expected exactly 1 network_error observation after exhaustion"
    )
