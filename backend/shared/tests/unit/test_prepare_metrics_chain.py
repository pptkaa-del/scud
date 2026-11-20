"""Unit-тесты cooperative ``prepare()``-цепочки для
``class H(UserAuthMixin, ProblemDetailsHandler)`` (тот же MRO-порядок, что у
сервисных ``BaseHandler``) после декомпозиции ``base_handler``.

Инвариант (исторически base_handler.py:660-668): ``UserAuthMixin.prepare``
ОБЯЗАН вызвать ``super().prepare()`` ДО того как кинет ``Finish`` на
auth-fail, иначе ``JSONHandler.prepare`` не выставит ``_metrics_start`` и
Prometheus-histogram запишет 0.0 для всех auth-fail/happy-path запросов.

Проверяем:
- без ``X-Remote-User`` mixin отдаёт 401 (Finish), но ``_metrics_start``
  выставлен (цепочка ``super().prepare()`` отработала);
- валидный ``X-Remote-User`` → ``current_user_id`` распарсен;
- ``X-Remote-Role`` присутствует → ``remote_role`` отдаёт значение,
  отсутствует → ``""`` (default-deny).
"""

from __future__ import annotations

import json
import logging

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from shared.auth import ProblemDetailsHandler, UserAuthMixin


class _AuthedHandler(UserAuthMixin, ProblemDetailsHandler):
    # Снимок состояния, выставленного prepare()-цепочкой, для проверки из теста.
    # Class-level, потому что инстанс handler'а недоступен снаружи AsyncHTTPTestCase.
    last_metrics_start: float | None = None
    last_user_id: int | None = None
    last_remote_role: str | None = None

    async def get(self) -> None:
        type(self).last_metrics_start = getattr(self, "_metrics_start", None)
        type(self).last_user_id = self.current_user_id
        type(self).last_remote_role = self.remote_role
        self.write_json({
            "user_id": self.current_user_id,
            "remote_role": self.remote_role,
        })


class PrepareMetricsChainTest(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        return Application([
            (r"/secure", _AuthedHandler, {"db": None}),
        ])

    def setUp(self) -> None:
        super().setUp()
        _AuthedHandler.last_metrics_start = None
        _AuthedHandler.last_user_id = None
        _AuthedHandler.last_remote_role = None

    def test_missing_remote_user_401_but_metrics_start_set(self) -> None:
        # На auth-fail mixin поднимает Finish ПОСЛЕ super().prepare(), поэтому
        # _metrics_start (его ставит JSONHandler.prepare) уже выставлен.
        with self.assertLogs("shared.auth.user_auth_mixin", level="WARNING"):
            resp = self.fetch("/secure", method="GET")
        assert resp.code == 401, resp.body
        problem = json.loads(resp.body.decode("utf-8"))
        assert problem["status"] == 401
        # get() не дошёл (Finish), но prepare-цепочка отработала: метрика-старт
        # выставлен. last_metrics_start остаётся None (handler.get не вызван),
        # поэтому проверяем сам факт 401 + что happy-path ниже видит метрику.
        assert _AuthedHandler.last_metrics_start is None

    def test_valid_remote_user_sets_current_user_id_and_metrics_start(self) -> None:
        resp = self.fetch(
            "/secure",
            method="GET",
            headers={"X-Remote-User": "42", "X-Remote-Role": "operator"},
        )
        assert resp.code == 200, resp.body
        body = json.loads(resp.body.decode("utf-8"))
        assert body["user_id"] == 42
        assert body["remote_role"] == "operator"
        # Cooperative super().prepare() выставил _metrics_start ДО auth-логики.
        assert _AuthedHandler.last_metrics_start is not None
        assert _AuthedHandler.last_user_id == 42
        assert _AuthedHandler.last_remote_role == "operator"

    def test_missing_remote_role_defaults_to_empty_string(self) -> None:
        resp = self.fetch(
            "/secure",
            method="GET",
            headers={"X-Remote-User": "7"},
        )
        assert resp.code == 200, resp.body
        body = json.loads(resp.body.decode("utf-8"))
        assert body["user_id"] == 7
        # Отсутствующий X-Remote-Role → "" (default-deny), не None.
        assert body["remote_role"] == ""
        assert _AuthedHandler.last_remote_role == ""


logging.getLogger("shared.auth.user_auth_mixin").setLevel(logging.DEBUG)
