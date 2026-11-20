"""Unit-тесты ретрай-семантики shared.http.RetryHTTPClient (COR-11).

POST, который таймаутнул ПОСЛЕ того, как upstream его уже обработал — это
ровно неоднозначный случай: повтор → дубликат OTP/SMS/security-event/
notification. Поэтому ретрай таймаутов/5xx разрешён по умолчанию только для
идемпотентных методов (GET/HEAD/PUT/DELETE). POST по умолчанию НЕ ретраит
таймаут/5xx; нужен явный opt-in idempotent=True. Connection-refused (заведомо
не доставлено) для POST ретраить безопасно — он не неоднозначен.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest import mock
from unittest.mock import AsyncMock, patch

from tornado.httpclient import HTTPError, HTTPRequest, HTTPResponse

from shared.http.retry_client import RetryHTTPClient


def _ok_response(method: str) -> HTTPResponse:
    return HTTPResponse(HTTPRequest(url="http://svc/x", method=method), 200)


def _timeout_error() -> HTTPError:
    # 599 — Tornado-код для timeout/connection failure.
    return HTTPError(599, message="Timeout")


def _server_error() -> HTTPError:
    return HTTPError(503, message="Service Unavailable")


class _FailNTimes:
    """Кидает заданное исключение первые n_fail вызовов, затем отдаёт ok-ответ.

    Синхронный side_effect для AsyncMock: возвращаемое значение AsyncMock сам
    обернёт в awaitable, а raise — пробросит. Async-функция тут привела бы к
    'coroutine was never awaited' (двойная обёртка)."""

    def __init__(
        self,
        exc_factory: Callable[[], BaseException],
        n_fail: int,
        ok_method: str,
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


def _run(client: RetryHTTPClient, fake: _FailNTimes, *, method: str) -> HTTPResponse:
    async def scenario() -> HTTPResponse:
        with patch("shared.http.retry_client.AsyncHTTPClient") as cls:
            cls.return_value.fetch = AsyncMock(side_effect=fake)
            return await client.fetch("http://svc/x", method=method)
    return asyncio.run(scenario())


# --- POST timeout: НЕ ретраится по умолчанию ----------------------------------

def test_post_timeout_not_retried_by_default() -> None:
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    fake = _FailNTimes(_timeout_error, n_fail=5, ok_method="POST")
    raised = False
    try:
        _run(client, fake, method="POST")
    except HTTPError:
        raised = True
    assert raised, "POST timeout must surface, not be swallowed by retry"
    assert fake.calls == 1, "POST timeout must NOT be retried by default"


def test_post_5xx_not_retried_by_default() -> None:
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    fake = _FailNTimes(_server_error, n_fail=5, ok_method="POST")
    raised = False
    try:
        _run(client, fake, method="POST")
    except HTTPError:
        raised = True
    assert raised
    assert fake.calls == 1, "POST 5xx must NOT be retried by default"


# --- GET timeout: ретраится (идемпотентный метод) -----------------------------

def test_get_timeout_is_retried() -> None:
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    # Падает дважды, на 3-й попытке успех.
    fake = _FailNTimes(_timeout_error, n_fail=2, ok_method="GET")
    resp = _run(client, fake, method="GET")
    assert resp.code == 200
    assert fake.calls == 3, "GET timeout must be retried"


# --- POST idempotent opt-in: ретраится ----------------------------------------

def test_post_timeout_retried_with_idempotent_optin() -> None:
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    fake = _FailNTimes(_timeout_error, n_fail=2, ok_method="POST")

    async def scenario() -> HTTPResponse:
        with patch("shared.http.retry_client.AsyncHTTPClient") as cls:
            cls.return_value.fetch = AsyncMock(side_effect=fake)
            return await client.fetch("http://svc/x", method="POST", idempotent=True)

    resp = asyncio.run(scenario())
    assert resp.code == 200
    assert fake.calls == 3, "idempotent POST must be retried"


# --- POST connection-refused: ретраится (заведомо не доставлено) ---------------

def test_post_connection_refused_is_retried() -> None:
    """connection-refused → запрос ТОЧНО не дошёл до upstream, повтор безопасен."""
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)

    def _refused() -> BaseException:
        return ConnectionRefusedError("Connection refused")

    fake = _FailNTimes(_refused, n_fail=2, ok_method="POST")
    resp = _run(client, fake, method="POST")
    assert resp.code == 200
    assert fake.calls == 3, "connection-refused POST is unambiguous → retry"


# --- GET/PUT/DELETE поведение не изменилось -----------------------------------

def test_put_timeout_is_retried() -> None:
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)
    fake = _FailNTimes(_timeout_error, n_fail=2, ok_method="PUT")
    resp = _run(client, fake, method="PUT")
    assert resp.code == 200
    assert fake.calls == 3


def test_4xx_never_retried() -> None:
    client = RetryHTTPClient(backoff_base=0.0, max_attempts=3)

    def _bad_request() -> BaseException:
        return HTTPError(400, message="Bad Request")

    fake = _FailNTimes(_bad_request, n_fail=5, ok_method="GET")
    raised = False
    try:
        _run(client, fake, method="GET")
    except HTTPError:
        raised = True
    assert raised
    assert fake.calls == 1, "4xx is a caller error, never retried"


# --- X-Request-ID propagation (A3) -------------------------------------------

def test_request_id_injected_from_contextvar() -> None:
    """X-Request-ID из ContextVar авто-добавляется в исходящий HTTPRequest."""
    from shared.observability import reset_request_id, set_request_id

    captured: dict[str, dict[str, str]] = {}

    class _FakeClient:
        async def fetch(self, req: HTTPRequest, raise_error: bool = True) -> HTTPResponse:
            captured["headers"] = dict(req.headers)
            return _ok_response(req.method)

    async def _run() -> None:
        token = set_request_id("ctx-rid-99")
        try:
            client = RetryHTTPClient()
            with mock.patch(
                "shared.http.retry_client.AsyncHTTPClient",
                return_value=_FakeClient(),
            ):
                await client.fetch("http://user_api:8000/x", method="GET")
        finally:
            reset_request_id(token)

    asyncio.run(_run())
    assert captured["headers"].get("X-Request-ID") == "ctx-rid-99"


def test_caller_request_id_header_not_overwritten() -> None:
    """Явный X-Request-ID вызывающего имеет приоритет над ContextVar."""
    from shared.observability import reset_request_id, set_request_id

    captured: dict[str, dict[str, str]] = {}

    class _FakeClient:
        async def fetch(self, req: HTTPRequest, raise_error: bool = True) -> HTTPResponse:
            captured["headers"] = dict(req.headers)
            return _ok_response(req.method)

    async def _run() -> None:
        token = set_request_id("ctx-id")
        try:
            client = RetryHTTPClient()
            with mock.patch(
                "shared.http.retry_client.AsyncHTTPClient",
                return_value=_FakeClient(),
            ):
                await client.fetch(
                    "http://user_api:8000/x",
                    method="GET",
                    headers={"X-Request-ID": "explicit-id"},
                )
        finally:
            reset_request_id(token)

    asyncio.run(_run())
    assert captured["headers"]["X-Request-ID"] == "explicit-id"
