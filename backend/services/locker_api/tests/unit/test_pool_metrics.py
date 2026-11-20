"""Unit-тесты pull-based DB-pool-saturation метрик (shared.metrics + connection).

Живёт в locker_api-сьюте (а не shared_tests): shared_tests-образ НЕ несёт
prometheus_client (shared/Dockerfile.tests его не ставит), тогда как
locker_api_tests — несёт. И shared.metrics, и shared.database импортируют
prometheus_client / psycopg2 на уровне модуля, поэтому покрываются именно здесь.

Покрываем:
- update_pool_gauges() читает provider() и проставляет 4 gauge по label
  {service, pool};
- update_pool_gauges() не падает на закрытом пуле ({'status': 'closed'}) и
  на provider, бросающем исключение (мета-метрика не должна валить scrape);
- get_pool_info() рапортует in_use/idle/waiters/database (saturation);
- _acquire_and_run инкрементит/декрементит _pool_waiters БЕЗ утечки счётчика.
"""
from __future__ import annotations

import asyncio
from typing import Any

from shared.database.connection import AsyncPostgres
from shared.database.errors import ConnectionPoolExhaustedError
from shared.metrics import (
    SERVICE_NAME,
    db_pool_connections_idle,
    db_pool_connections_in_use,
    db_pool_connections_max,
    db_pool_connections_waiters,
    register_pool,
    update_pool_gauges,
)

# --- update_pool_gauges ---------------------------------------------------------

def test_update_pool_gauges_sets_values() -> None:
    """provider() с числовыми ключами → каждый gauge выставлен по {service, pool}."""
    def provider() -> dict[str, Any]:
        return {
            "in_use": 3,
            "idle": 2,
            "waiters": 1,
            "maxconn": 20,
            "database": "scud_test",
        }

    register_pool("scud_test", provider)
    update_pool_gauges()

    assert db_pool_connections_in_use.labels(
        service=SERVICE_NAME, pool="scud_test",
    )._value.get() == 3
    assert db_pool_connections_idle.labels(
        service=SERVICE_NAME, pool="scud_test",
    )._value.get() == 2
    assert db_pool_connections_waiters.labels(
        service=SERVICE_NAME, pool="scud_test",
    )._value.get() == 1
    assert db_pool_connections_max.labels(
        service=SERVICE_NAME, pool="scud_test",
    )._value.get() == 20


def test_update_pool_gauges_skips_closed_pool() -> None:
    """Закрытый пул отдаёт {'status': 'closed'} без числовых ключей → no-op, без падения."""
    register_pool("scud_closed", lambda: {"status": "closed"})
    update_pool_gauges()  # не должно поднять исключение


def test_update_pool_gauges_swallows_provider_exception() -> None:
    """Провайдер, бросающий исключение, не должен пробрасывать его в scrape."""
    def boom() -> dict[str, Any]:
        raise RuntimeError("provider exploded")

    register_pool("scud_boom", boom)
    update_pool_gauges()  # мета-метрика глотает ошибку провайдера


# --- get_pool_info saturation ---------------------------------------------------

class _FakePool:
    """Псевдо-ThreadedConnectionPool: _used (dict) и _pool (list) как в psycopg2 2.9."""

    def __init__(self, used: int, idle: int, maxconn: int) -> None:
        self._used = dict.fromkeys(range(used), object())
        self._pool = list(range(idle))
        self._maxconn = maxconn
        self._minconn = 1
        self._closed = False


class _FakeConfig:
    database = "scud_probe"


class _PoolInfoProbe(AsyncPostgres):
    """AsyncPostgres без реального коннекта: подставляем фейковый пул/конфиг."""

    def __init__(self, used: int, idle: int, maxconn: int) -> None:
        # Не зовём super().__init__ — он строит ThreadedConnectionPool (коннект к БД).
        self._pool = _FakePool(used, idle, maxconn)  # type: ignore[assignment]
        self._config = _FakeConfig()  # type: ignore[assignment]
        self._pool_waiters = 0
        self._closed = False


def test_get_pool_info_reports_saturation() -> None:
    """get_pool_info() рапортует in_use/idle/waiters/database из живого пула."""
    probe = _PoolInfoProbe(used=2, idle=4, maxconn=20)
    info = probe.get_pool_info()
    assert info["in_use"] == 2
    assert info["idle"] == 4
    assert info["waiters"] == 0
    assert info["database"] == "scud_probe"
    assert info["maxconn"] == 20
    # Историческое поле сохранено для обратной совместимости.
    assert info["pool_size"] == 4


# --- _pool_waiters increment/decrement -----------------------------------------

class _WaiterProbe(AsyncPostgres):
    """AsyncPostgres с управляемым _acquire_and_run-исполнителем: первый проход
    кидает ConnectionPoolExhaustedError (форсирует re-poll-ветку с await), второй
    отдаёт результат. Проверяем, что _pool_waiters возвращается к 0."""

    def __init__(self) -> None:
        self._pool_waiters = 0
        self._pool_acquire_timeout_sec = 5.0
        self._executor = None  # fake run_in_executor его игнорирует
        self._closed = False


def test_pool_waiters_increment_decrement() -> None:
    """Re-poll-ветка инкрементит счётчик ожидающих и обязательно его декрементит."""
    async def scenario() -> None:
        probe = _WaiterProbe()
        calls = {"n": 0}
        max_seen = {"waiters": 0}

        async def fake_run_in_executor(_executor: Any, fn: Any) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                # На первом проходе фиксируем, что счётчик ожидающих поднят.
                max_seen["waiters"] = max(max_seen["waiters"], probe._pool_waiters)
                raise ConnectionPoolExhaustedError("pool busy")
            return 42

        async def fake_sleep(_delay: float) -> None:
            # Во время await sleep счётчик ожидающих должен быть > 0.
            max_seen["waiters"] = max(max_seen["waiters"], probe._pool_waiters)

        import shared.database.connection as conn_mod

        class _FakeIOLoop:
            def run_in_executor(self, executor: Any, fn: Any) -> Any:
                return fake_run_in_executor(executor, fn)

        orig_ioloop = conn_mod.IOLoop.current
        orig_sleep = conn_mod.asyncio.sleep
        conn_mod.IOLoop.current = staticmethod(lambda: _FakeIOLoop())  # type: ignore[assignment]
        conn_mod.asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            result = await probe._acquire_and_run("probe_op", lambda: 42, ())
        finally:
            conn_mod.IOLoop.current = orig_ioloop  # type: ignore[assignment]
            conn_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]

        assert result == 42
        assert max_seen["waiters"] >= 1, "счётчик ожидающих не поднимался в re-poll"
        assert probe._pool_waiters == 0, "утечка счётчика ожидающих"

    asyncio.run(scenario())
