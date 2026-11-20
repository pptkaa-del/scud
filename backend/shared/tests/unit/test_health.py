"""Юнит-тесты shared.auth.health: /live, /ready, /health + probe-фабрики.

Контракт:
  - LivenessHandler → 200 {"status":"alive"}, БЕЗ X-Request-ID (минует
    prepare()/on_finish JSONHandler — никаких http-метрик/корреляции);
  - ReadinessHandler: all-ok → 200/ok; soft-fail → 200/degraded + ключ
    "unavailable"; critical(db)-fail → 503/unavailable; пробы исполняются
    конкурентно и НИКОГДА не роняют /ready (raising-проба = провал);
  - probe-фабрики: db_check critical=True, redis/mqtt critical=False,
    каждая глушит исключение → False.
"""
from __future__ import annotations

import asyncio
import json
from typing import ClassVar

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from shared.auth import (
    HealthHandler,
    LivenessHandler,
    ReadinessCheck,
    ReadinessHandler,
    db_check,
    mqtt_check,
    redis_check,
)


async def _ok() -> bool:
    return True


async def _fail() -> bool:
    return False


async def _raises() -> bool:
    raise RuntimeError("probe blew up")


class _FakeDb:
    """Минимальный фейк AsyncPostgres.fetch_one для HealthHandler."""

    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    async def fetch_one(self, query: str, read_only: bool = False) -> dict[str, int]:
        if self._fail:
            raise RuntimeError("db down")
        return {"?column?": 1}


# --- handler-тесты (tornado.testing) ----------------------------------------


class LivenessHandlerTest(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        return Application([(r"/live", LivenessHandler)])

    def test_live_returns_200_alive(self) -> None:
        resp = self.fetch("/live")
        assert resp.code == 200, resp.body
        assert json.loads(resp.body) == {"status": "alive"}

    def test_live_does_not_mint_request_id(self) -> None:
        # LivenessHandler минует JSONHandler.prepare() — X-Request-ID не минтится.
        resp = self.fetch("/live")
        assert resp.headers.get("X-Request-ID") is None, dict(resp.headers)


class ReadinessHandlerTest(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        return Application([
            (r"/ready-ok", ReadinessHandler, {
                "checks": [
                    ReadinessCheck(name="db", probe=_ok, critical=True),
                    ReadinessCheck(name="redis", probe=_ok, critical=False),
                ],
            }),
            (r"/ready-soft", ReadinessHandler, {
                "checks": [
                    ReadinessCheck(name="db", probe=_ok, critical=True),
                    ReadinessCheck(name="redis", probe=_fail, critical=False),
                ],
            }),
            (r"/ready-critical", ReadinessHandler, {
                "checks": [
                    ReadinessCheck(name="db", probe=_fail, critical=True),
                    ReadinessCheck(name="redis", probe=_ok, critical=False),
                ],
            }),
            (r"/ready-raises", ReadinessHandler, {
                "checks": [
                    ReadinessCheck(name="db", probe=_raises, critical=True),
                ],
            }),
            (r"/ready-both-fail", ReadinessHandler, {
                "checks": [
                    ReadinessCheck(name="db", probe=_fail, critical=True),
                    ReadinessCheck(name="redis", probe=_fail, critical=False),
                ],
            }),
        ])

    def test_all_ok_returns_200_ok(self) -> None:
        resp = self.fetch("/ready-ok")
        assert resp.code == 200, resp.body
        body = json.loads(resp.body)
        assert body["status"] == "ok"
        assert body["db"] == "ok"
        assert body["redis"] == "ok"

    def test_soft_fail_returns_200_degraded(self) -> None:
        resp = self.fetch("/ready-soft")
        assert resp.code == 200, resp.body
        body = json.loads(resp.body)
        assert body["status"] == "degraded"
        assert body["db"] == "ok"
        assert body["redis"] == "unavailable"

    def test_critical_fail_returns_503_unavailable(self) -> None:
        resp = self.fetch("/ready-critical")
        assert resp.code == 503, resp.body
        body = json.loads(resp.body)
        assert body["status"] == "unavailable"
        assert body["db"] == "unavailable"
        assert body["redis"] == "ok"

    def test_raising_probe_is_treated_as_failure_not_500(self) -> None:
        # Проба, поднявшая исключение, не роняет /ready: critical-fail → 503.
        resp = self.fetch("/ready-raises")
        assert resp.code == 503, resp.body
        body = json.loads(resp.body)
        assert body["status"] == "unavailable"
        assert body["db"] == "unavailable"

    def test_critical_dominates_soft_when_both_fail(self) -> None:
        # Одновременный провал CRITICAL и SOFT → 503/unavailable (не 200/degraded).
        resp = self.fetch("/ready-both-fail")
        assert resp.code == 503, resp.body
        body = json.loads(resp.body)
        assert body["status"] == "unavailable"
        assert body["db"] == "unavailable"
        assert body["redis"] == "unavailable"


class _ConcurrencyHandlerTest(AsyncHTTPTestCase):
    """Пробы исполняются конкурентно (asyncio.gather), а не последовательно."""

    order: ClassVar[list[str]] = []

    def get_app(self) -> Application:
        async def slow() -> bool:
            type(self).order.append("slow-start")
            await asyncio.sleep(0.05)
            type(self).order.append("slow-end")
            return True

        async def fast() -> bool:
            type(self).order.append("fast-start")
            type(self).order.append("fast-end")
            return True

        return Application([
            (r"/ready-conc", ReadinessHandler, {
                "checks": [
                    ReadinessCheck(name="slow", probe=slow, critical=False),
                    ReadinessCheck(name="fast", probe=fast, critical=False),
                ],
            }),
        ])

    def setUp(self) -> None:
        super().setUp()
        type(self).order = []

    def test_probes_run_concurrently(self) -> None:
        resp = self.fetch("/ready-conc")
        assert resp.code == 200, resp.body
        # fast стартует и финиширует ВНУТРИ окна ожидания slow → конкурентно.
        assert type(self).order.index("fast-end") < type(self).order.index("slow-end")


class HealthHandlerTest(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        return Application([
            (r"/health-ok", HealthHandler, {"db": _FakeDb(fail=False)}),
            (r"/health-down", HealthHandler, {"db": _FakeDb(fail=True)}),
        ])

    def test_db_ok_returns_200(self) -> None:
        resp = self.fetch("/health-ok")
        assert resp.code == 200, resp.body
        assert json.loads(resp.body) == {"status": "ok", "db": "ok"}

    def test_db_down_returns_503_degraded(self) -> None:
        resp = self.fetch("/health-down")
        assert resp.code == 503, resp.body
        assert json.loads(resp.body) == {"status": "degraded", "db": "unavailable"}


# --- probe-фабрики (asyncio.run, без HTTP) ----------------------------------


def test_db_check_is_critical_and_ok() -> None:
    check = db_check(_FakeDb(fail=False))
    assert check.name == "db"
    assert check.critical is True
    assert asyncio.run(check.probe()) is True


def test_db_check_swallows_exception_to_false() -> None:
    check = db_check(_FakeDb(fail=True))
    assert asyncio.run(check.probe()) is False


class _FakeRedis:
    def __init__(self, *, fail: bool, value: bool = True) -> None:
        self._fail = fail
        self._value = value

    async def ping(self) -> bool:
        if self._fail:
            raise RuntimeError("redis down")
        return self._value


def test_redis_check_is_soft_and_ok() -> None:
    check = redis_check(_FakeRedis(fail=False))
    assert check.name == "redis"
    assert check.critical is False
    assert asyncio.run(check.probe()) is True


def test_redis_check_swallows_exception_to_false() -> None:
    check = redis_check(_FakeRedis(fail=True))
    assert asyncio.run(check.probe()) is False


class _FakeMqtt:
    def __init__(self, *, connected: bool, raises: bool = False) -> None:
        self._connected = connected
        self._raises = raises

    @property
    def is_connected(self) -> bool:
        if self._raises:
            raise RuntimeError("mqtt state error")
        return self._connected


def test_mqtt_check_is_soft_and_reflects_connection() -> None:
    check = mqtt_check(_FakeMqtt(connected=True))
    assert check.name == "mqtt"
    assert check.critical is False
    assert asyncio.run(check.probe()) is True
    assert asyncio.run(mqtt_check(_FakeMqtt(connected=False)).probe()) is False


def test_mqtt_check_swallows_exception_to_false() -> None:
    check = mqtt_check(_FakeMqtt(connected=True, raises=True))
    assert asyncio.run(check.probe()) is False
