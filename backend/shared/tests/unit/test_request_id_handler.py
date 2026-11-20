"""Захват/генерация/эхо X-Request-ID в JSONHandler + reset ContextVar.

Покрывает:
  - успешный путь (write_json) — заголовок эхируется;
  - путь ошибки (write_error / write_problem via ProblemDetailsHandler) —
    заголовок должен присутствовать несмотря на Tornado internal clear().
"""
from __future__ import annotations

import json

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from shared.auth import JSONHandler, ProblemDetailsHandler
from shared.observability import get_request_id


class _Echo(JSONHandler):
    seen_request_id: str | None = None

    async def get(self) -> None:
        type(self).seen_request_id = self.request_id
        self.write_json({"rid": self.request_id})


class _ValidationError(ProblemDetailsHandler):
    """Handler, который всегда отдаёт 400 через write_problem (Problem Details)."""

    async def get(self) -> None:
        self.write_validation_error("intentional test error")


class _RaisesValueError(ProblemDetailsHandler):
    """Handler, который поднимает ValueError → write_error → 500."""

    async def get(self) -> None:
        raise ValueError("test bug — should not leak to client")


class RequestIdHandlerTest(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        return Application([
            (r"/echo", _Echo, {"db": None}),
            (r"/val-error", _ValidationError, {"db": None}),
            (r"/raises", _RaisesValueError, {"db": None}),
        ])

    def setUp(self) -> None:
        super().setUp()
        _Echo.seen_request_id = None

    def test_incoming_header_is_captured_and_echoed(self) -> None:
        resp = self.fetch("/echo", headers={"X-Request-ID": "client-rid-42"})
        assert resp.code == 200, resp.body
        assert _Echo.seen_request_id == "client-rid-42"
        assert resp.headers.get("X-Request-ID") == "client-rid-42"
        assert json.loads(resp.body)["rid"] == "client-rid-42"

    def test_missing_header_generates_id(self) -> None:
        resp = self.fetch("/echo")
        assert resp.code == 200, resp.body
        assert resp.headers.get("X-Request-ID")
        assert len(resp.headers["X-Request-ID"]) == 32

    def test_contextvar_reset_after_request(self) -> None:
        self.fetch("/echo", headers={"X-Request-ID": "leak-check"})
        # После завершения запроса ContextVar в тест-корутине не утёк.
        assert get_request_id() == ""

    # --- error-path: X-Request-ID должен присутствовать на 4xx/5xx ----------

    def test_problem_details_400_echoes_request_id(self) -> None:
        """write_problem (Problem Details 400) должен нести X-Request-ID."""
        resp = self.fetch("/val-error", headers={"X-Request-ID": "err-rid-99"})
        assert resp.code == 400, resp.body
        assert resp.headers.get("X-Request-ID") == "err-rid-99", (
            f"X-Request-ID missing on 400 response; headers={dict(resp.headers)}"
        )
        body = json.loads(resp.body)
        assert body["status"] == 400

    def test_problem_details_400_generates_id_when_missing(self) -> None:
        """write_problem без входящего X-Request-ID — генерирует и эхирует."""
        resp = self.fetch("/val-error")
        assert resp.code == 400, resp.body
        rid = resp.headers.get("X-Request-ID")
        assert rid, f"X-Request-ID not generated on 400; headers={dict(resp.headers)}"
        assert len(rid) == 32, f"unexpected rid length: {rid!r}"

    def test_write_error_500_echoes_request_id(self) -> None:
        """Необработанный ValueError → write_error 500 должен нести X-Request-ID."""
        resp = self.fetch("/raises", headers={"X-Request-ID": "err-rid-500"})
        assert resp.code == 500, resp.body
        assert resp.headers.get("X-Request-ID") == "err-rid-500", (
            f"X-Request-ID missing on 500 response; headers={dict(resp.headers)}"
        )
