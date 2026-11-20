"""Unit-тесты инкремента scud_db_errors_total из AsyncPostgres.

Живёт в locker_api-сьюте (psycopg2 доступен). Гоняем _run_with_retry_async
через __new__-инстанс (без реального пула), подменяя _acquire_and_run на
async-функцию, которая кидает нужное исключение, и проверяем дельту счётчика."""
from __future__ import annotations

import asyncio
import contextlib

import psycopg2

from shared.database.connection import AsyncPostgres
from shared.database.errors import ConnectionPoolExhaustedError
from shared.metrics import SERVICE_NAME, db_errors_total


def _make_db() -> AsyncPostgres:
    db = AsyncPostgres.__new__(AsyncPostgres)
    db._retries = 1
    db._retry_delay_sec = 0.0
    db._retry_backoff_multiplier = 2.0
    db._max_retry_delay_sec = 0.0
    db._max_total_time_sec = None
    db._executor = None  # pool test patches run_in_executor; executor arg unused
    db._pool_waiters = 0
    return db


def _err_value(operation: str, error_code: str) -> float:
    return db_errors_total.labels(
        service=SERVICE_NAME, operation=operation, error_code=error_code,
    )._value.get()


def test_psycopg2_error_increments_db_errors_with_pgcode() -> None:
    db = _make_db()

    class _SerFailure(psycopg2.OperationalError):
        pgcode = "40001"  # serialization_failure (non-retryable here w/ write)

    before = _err_value("execute", "40001")

    async def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise _SerFailure("serialization failure")

    db._acquire_and_run = boom  # type: ignore[method-assign]

    async def scenario() -> None:
        with contextlib.suppress(psycopg2.Error):
            await db._run_with_retry_async(
                "execute", lambda *a: None, "SQL", None,
                read_only=False, idempotent=False,
            )

    asyncio.run(scenario())
    assert _err_value("execute", "40001") == before + 1


def test_psycopg2_error_without_pgcode_uses_unknown() -> None:
    db = _make_db()
    before = _err_value("fetch_all", "unknown")

    async def boom(*a, **kw):  # type: ignore[no-untyped-def]
        exc = psycopg2.ProgrammingError("syntax error")
        assert getattr(exc, "pgcode", None) is None
        raise exc

    db._acquire_and_run = boom  # type: ignore[method-assign]

    async def scenario() -> None:
        with contextlib.suppress(psycopg2.Error):
            await db._run_with_retry_async(
                "fetch_all", lambda *a: None, "SQL", None, True,
                read_only=True, idempotent=False,
            )

    asyncio.run(scenario())
    assert _err_value("fetch_all", "unknown") == before + 1


def test_pool_exhausted_increments_db_errors() -> None:
    db = _make_db()
    db._pool_acquire_timeout_sec = 0.0  # бюджет исчерпан немедленно
    before = _err_value("execute", "pool_exhausted")

    async def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise ConnectionPoolExhaustedError("connection pool exhausted")

    # run_in_executor подменяем напрямую — гоняем именно _acquire_and_run.
    from tornado.ioloop import IOLoop

    async def scenario() -> None:
        # Подменяем executor-вызов внутри _acquire_and_run на raise.
        orig = IOLoop.current().run_in_executor
        async def fake_run_in_executor(executor, fn):  # type: ignore[no-untyped-def]
            raise ConnectionPoolExhaustedError("connection pool exhausted")
        IOLoop.current().run_in_executor = fake_run_in_executor  # type: ignore[assignment]
        try:
            await db._acquire_and_run("execute", lambda *a: None, ())
        except ConnectionPoolExhaustedError:
            pass
        finally:
            IOLoop.current().run_in_executor = orig  # type: ignore[assignment]

    asyncio.run(scenario())
    assert _err_value("execute", "pool_exhausted") == before + 1
