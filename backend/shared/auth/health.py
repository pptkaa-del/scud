"""Health-depth хендлеры и probe-фабрики (единый источник семантики).

Заменяет 7 копий per-service HealthHandler и вводит /live + /ready:
- LivenessHandler — «процесс жив», БЕЗ БД, БЕЗ JSONHandler (минует
  prepare()/on_finish: liveness-проба не должна минтить X-Request-ID,
  открывать БД-пул или писать http-метрики — это самый дешёвый probe).
- ReadinessHandler — «готов принимать трафик»: запускает набор
  ReadinessCheck конкурентно (asyncio.gather, return_exceptions=True),
  любой critical-fail → 503/unavailable; мягкий fail → 200/degraded.
- HealthHandler — обратная совместимость со старым /health (db SELECT 1).

Все пробы try/except→bool: проба НИКОГДА не поднимает исключение.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
import logging
from typing import Any, Protocol, runtime_checkable

from tornado.web import RequestHandler

from ..database import AsyncPostgres
from .json_handler import JSONHandler

logger = logging.getLogger(__name__)

_STATUS_OK = "ok"
_STATUS_DEGRADED = "degraded"
_STATUS_UNAVAILABLE = "unavailable"
_STATUS_ALIVE = "alive"

_HTTP_OK = 200
_HTTP_UNAVAILABLE = 503

# ---------------------------------------------------------------------------
# Structural protocols for probe-factory params.
# Using Protocol lets structural fakes (tests, mocks) pass mypy-strict
# without cast() or type: ignore.  Public factory names are unchanged.
# ---------------------------------------------------------------------------


@runtime_checkable
class _DbProto(Protocol):
    """Structural subset of AsyncPostgres needed by probe factories."""

    async def fetch_one(self, query: str, read_only: bool = ...) -> Any: ...


@runtime_checkable
class _RedisProto(Protocol):
    """Structural subset of RedisClient needed by probe factories."""

    async def ping(self) -> bool: ...


@runtime_checkable
class _MqttProto(Protocol):
    """Structural subset of MqttClient needed by probe factories."""

    @property
    def is_connected(self) -> bool: ...


# Awaitable return type produced by each probe coroutine.
_ProbeResult = Coroutine[Any, Any, bool]

# Callable type for a readiness probe: async () -> bool.
# Uses Coroutine (not bare Awaitable) so asyncio.run() and asyncio.gather()
# see the precise return type without widening to Awaitable[Unknown].
_Probe = Callable[[], _ProbeResult]


@dataclass(frozen=True)
class ReadinessCheck:
    """Единичная проба готовности.

    name:     ключ в JSON-ответе /ready ("db", "redis", "mqtt", ...).
    probe:    async () -> bool; True = здорова. НИКОГДА не поднимает.
    critical: True → провал отдаёт 503/unavailable; False → 200/degraded.
    """

    name: str
    probe: _Probe
    critical: bool


class LivenessHandler(RequestHandler):
    """GET /live → 200 {"status": "alive"}.

    Наследует tornado.web.RequestHandler НАПРЯМУЮ (не JSONHandler):
    liveness-проба намеренно минует prepare()/on_finish, поэтому НЕ
    минтит X-Request-ID и НЕ пишет http-метрики. Без БД.
    """

    async def get(self) -> None:
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.set_status(_HTTP_OK)
        self.write({"status": _STATUS_ALIVE})


class ReadinessHandler(JSONHandler):
    """GET /ready → агрегирует набор ReadinessCheck.

    initialize() переопределён: /ready не работает с БД напрямую,
    поэтому НЕ принимает db (в отличие от JSONHandler.initialize).
    """

    checks: list[ReadinessCheck]

    def initialize(self, checks: list[ReadinessCheck]) -> None:  # type: ignore[override]
        self.checks = checks

    async def get(self) -> None:
        results = await asyncio.gather(
            *(check.probe() for check in self.checks),
            return_exceptions=True,
        )

        body: dict[str, str] = {}
        critical_failed = False
        soft_failed = False
        for check, result in zip(self.checks, results, strict=True):
            healthy = result is True
            if not healthy:
                if isinstance(result, BaseException):
                    logger.warning(
                        "Readiness probe %s raised: %s", check.name, result,
                    )
                if check.critical:
                    critical_failed = True
                else:
                    soft_failed = True
            body[check.name] = _STATUS_OK if healthy else _STATUS_UNAVAILABLE

        if critical_failed:
            body["status"] = _STATUS_UNAVAILABLE
            self.write_json(body, status_code=_HTTP_UNAVAILABLE)
            return
        body["status"] = _STATUS_DEGRADED if soft_failed else _STATUS_OK
        self.write_json(body, status_code=_HTTP_OK)


class HealthHandler(JSONHandler):
    """GET /health → 200 {"status":"ok","db":"ok"} / 503 при недоступной БД.

    Единая замена 7 per-service копий. Сохраняет старый контракт.
    """

    db: AsyncPostgres

    def initialize(self, db: AsyncPostgres) -> None:
        self.db = db

    async def get(self) -> None:
        try:
            await self.db.fetch_one("SELECT 1", read_only=True)
        except Exception as e:
            logger.warning("DB health check failed: %s", e)
            self.write_json(
                {"status": _STATUS_DEGRADED, "db": _STATUS_UNAVAILABLE},
                status_code=_HTTP_UNAVAILABLE,
            )
            return
        self.write_json({"status": _STATUS_OK, "db": _STATUS_OK}, status_code=_HTTP_OK)


def db_check(db: _DbProto) -> ReadinessCheck:
    """Critical-проба: SELECT 1 read-only. Любая ошибка → False."""

    async def probe() -> bool:
        try:
            await db.fetch_one("SELECT 1", read_only=True)
            return True
        except Exception as e:
            logger.warning("db readiness probe failed: %s", e)
            return False

    return ReadinessCheck(name="db", probe=probe, critical=True)


def redis_check(redis: _RedisProto) -> ReadinessCheck:
    """Soft-проба: Redis PING. Любая ошибка → False."""

    async def probe() -> bool:
        try:
            return await redis.ping()
        except Exception as e:
            logger.warning("redis readiness probe failed: %s", e)
            return False

    return ReadinessCheck(name="redis", probe=probe, critical=False)


def mqtt_check(mqtt_client: _MqttProto) -> ReadinessCheck:
    """Soft-проба: брокер MQTT подключён (is_connected). Ошибка → False."""

    async def probe() -> bool:
        try:
            return bool(mqtt_client.is_connected)
        except Exception as e:
            logger.warning("mqtt readiness probe failed: %s", e)
            return False

    return ReadinessCheck(name="mqtt", probe=probe, critical=False)
