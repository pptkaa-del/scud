"""Unit-тесты ретрай-семантики shared.database (COR-10).

Неоднозначный сбой соединения (psycopg2.OperationalError без pgcode) может
прийти ПОСЛЕ того, как сервер уже применил INSERT/UPDATE/COMMIT, но ACK был
потерян. Слепой ретрай такой write-операции → молчаливый дубликат строки на
таблицах без unique-constraint (telemetry, security_events, notification logs)
→ порча audit-trail.

Семантика после фикса (см. AsyncPostgres._should_retry):
- read-only операции свободно ретраятся — у SELECT нет side-effect;
- write-операции (read_only=False) при неоднозначном сбое (pgcode is None)
  НЕ ретраятся по умолчанию — поднимаем ошибку, пусть caller разбирается
  (лучше явная ошибка, чем тихий дубликат);
- write с idempotent=True (ON CONFLICT / natural-key / CAS UPDATE) ретраится.

Живёт в locker_api-сьюте (а не shared_tests): shared_tests-образ не несёт
psycopg2, тогда как shared.database импортирует драйвер на уровне модуля
(см. test_shared_database.py).
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import time

import psycopg2
import pytest

from shared.database.connection import AsyncPostgres
from shared.database.errors import ConnectionPoolExhaustedError, RetryExhaustedError


def _make_db(retries: int = 3, *, pool_acquire_timeout_sec: float = 5.0) -> AsyncPostgres:
    """AsyncPostgres без реального пула: только поля, нужные ретрай-петле.

    Не зовём __init__ (он строит ThreadedConnectionPool к БД); проставляем
    вручную минимальный набор атрибутов, от которых зависит _run_with_retry_async.
    Реальный (но крошечный) ThreadPoolExecutor нужен для run_in_executor —
    sync_fn в тестах ходит не в БД, а просто кидает исключение.
    """
    db = AsyncPostgres.__new__(AsyncPostgres)
    db._closed = False
    db._retries = retries
    db._retry_delay_sec = 0.0
    db._retry_backoff_multiplier = 1.0
    db._max_retry_delay_sec = 0.0
    db._max_total_time_sec = None
    db._pool_acquire_timeout_sec = pool_acquire_timeout_sec
    db._pool_waiters = 0  # pool-saturation счётчик, читается в _acquire_and_run re-poll
    db._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-pg")
    return db


def _ambiguous_conn_drop() -> psycopg2.OperationalError:
    """Неоднозначный обрыв соединения: OperationalError без SQLSTATE (pgcode None)."""
    exc = psycopg2.OperationalError("server closed the connection unexpectedly")
    assert getattr(exc, "pgcode", None) is None
    return exc


# --- WRITE: неоднозначный сбой НЕ ретраится по умолчанию ----------------------

def test_write_ambiguous_failure_is_not_retried_by_default():
    """COR-10: write (read_only=False, idempotent не задан) при pgcode-None
    обрыве НЕ ретраится — sync_fn вызвана один раз, ошибка проброшена наружу."""
    async def scenario():
        db = _make_db()
        calls = {"n": 0}

        def sync_fn():
            calls["n"] += 1
            raise _ambiguous_conn_drop()

        with pytest.raises(psycopg2.OperationalError):
            await db._run_with_retry_async(
                "execute", sync_fn, read_only=False, idempotent=False,
            )
        assert calls["n"] == 1, "ambiguous write must NOT be retried"

    asyncio.run(scenario())


# --- READ: неоднозначный сбой ретраится (нет side-effect) ---------------------

def test_read_ambiguous_failure_is_retried():
    """SELECT (read_only=True) свободно ретраится — у чтения нет side-effect."""
    async def scenario():
        db = _make_db()
        calls = {"n": 0}

        def sync_fn():
            calls["n"] += 1
            raise _ambiguous_conn_drop()

        with pytest.raises(RetryExhaustedError):
            await db._run_with_retry_async(
                "fetch_all", sync_fn, read_only=True, idempotent=False,
            )
        assert calls["n"] == 3, "read must be retried up to _retries times"

    asyncio.run(scenario())


# --- WRITE idempotent=True: ретраится ----------------------------------------

def test_write_ambiguous_failure_is_retried_when_idempotent():
    """Идемпотентный write (ON CONFLICT / natural-key / CAS) ретраится явным opt-in."""
    async def scenario():
        db = _make_db()
        calls = {"n": 0}

        def sync_fn():
            calls["n"] += 1
            raise _ambiguous_conn_drop()

        with pytest.raises(RetryExhaustedError):
            await db._run_with_retry_async(
                "execute", sync_fn, read_only=False, idempotent=True,
            )
        assert calls["n"] == 3, "idempotent write must be retried"

    asyncio.run(scenario())


# --- _should_retry прямой контракт -------------------------------------------

def test_should_retry_write_ambiguous_false_default():
    db = AsyncPostgres.__new__(AsyncPostgres)
    assert db._should_retry(_ambiguous_conn_drop(), read_only=False, idempotent=False) is False


def test_should_retry_read_ambiguous_true():
    db = AsyncPostgres.__new__(AsyncPostgres)
    assert db._should_retry(_ambiguous_conn_drop(), read_only=True, idempotent=False) is True


def test_should_retry_write_idempotent_ambiguous_true():
    db = AsyncPostgres.__new__(AsyncPostgres)
    assert db._should_retry(_ambiguous_conn_drop(), read_only=False, idempotent=True) is True


# --- COR-22: pool-acquire бюджет отделён от SQLSTATE-ретраев -------------------

def test_pool_exhaustion_raises_pool_error_after_budget_not_retry_exhausted():
    """COR-22: затяжное исчерпание пула выдаёт ConnectionPoolExhaustedError
    по истечении pool_acquire_timeout_sec — НЕ RetryExhaustedError. Ожидание
    идёт по бюджету времени, а не по счётчику SQLSTATE-попыток."""
    async def scenario():
        # retries=3, но крошечный бюджет: пул всегда занят.
        db = _make_db(retries=3, pool_acquire_timeout_sec=0.3)
        calls = {"n": 0}

        def sync_fn():
            calls["n"] += 1
            raise ConnectionPoolExhaustedError("connection pool exhausted")

        start = time.monotonic()
        with pytest.raises(ConnectionPoolExhaustedError):
            await db._run_with_retry_async(
                "fetch_all", sync_fn, read_only=True, idempotent=False,
            )
        elapsed = time.monotonic() - start
        # Бюджет уважается: ждём примерно до budget, а не сваливаемся за ~0.35s
        # из-за исчерпания 3 SQLSTATE-попыток.
        assert elapsed >= 0.3, f"pool-acquire budget not honored: {elapsed:.3f}s"
        # Пул опрашивался многократно (короткие повторы), а не 3 раза.
        assert calls["n"] >= 3

    asyncio.run(scenario())


def test_pool_wait_does_not_consume_sqlstate_retry_budget():
    """COR-22: ожидание свободного соединения НЕ должно списывать SQLSTATE-
    попытки. После серии pool-exhausted'ов реальная retryable SQLSTATE-ошибка
    всё ещё получает полный бюджет _retries попыток."""
    async def scenario():
        db = _make_db(retries=3, pool_acquire_timeout_sec=5.0)
        # Сначала несколько pool-exhausted, затем retryable SQLSTATE-ошибка
        # на каждой реальной попытке. Если pool-wait списывал бы attempts,
        # реальных попыток осталось бы < 3.
        exec_attempts = {"n": 0}
        pool_misses = {"n": 0}

        class _AdminShutdown(psycopg2.OperationalError):
            pgcode = "57P01"  # retryable SQLSTATE (admin_shutdown)

        def sync_fn():
            if pool_misses["n"] < 4:
                pool_misses["n"] += 1
                raise ConnectionPoolExhaustedError("pool busy")
            exec_attempts["n"] += 1
            raise _AdminShutdown("admin shutdown")

        with pytest.raises(RetryExhaustedError):
            await db._run_with_retry_async(
                "fetch_all", sync_fn, read_only=True, idempotent=False,
            )
        assert exec_attempts["n"] == 3, (
            f"SQLSTATE retry budget consumed by pool waits: {exec_attempts['n']}"
        )

    asyncio.run(scenario())


def test_pool_exhaustion_then_success_within_budget():
    """COR-22: если соединение освобождается в пределах бюджета — операция
    выполняется успешно, без расхода SQLSTATE-попыток."""
    async def scenario():
        db = _make_db(retries=1, pool_acquire_timeout_sec=5.0)
        state = {"n": 0}

        def sync_fn():
            state["n"] += 1
            if state["n"] < 3:
                raise ConnectionPoolExhaustedError("pool busy")
            return [{"ok": 1}]

        rows = await db._run_with_retry_async(
            "fetch_all", sync_fn, read_only=True, idempotent=False,
        )
        assert rows == [{"ok": 1}]
        # retries=1 — единственная SQLSTATE-попытка не потрачена на pool-waits.

    asyncio.run(scenario())


# --- COR-31: retries=0 невозможен (это число попыток, не доп-ретраев) ----------

def test_retries_zero_rejected_at_construction():
    """COR-31: retries — это число ПОПЫТОК; 0 попыток = операция никогда не
    выполнится. Конструктор должен отвергать retries < 1."""
    config = _minimal_config()
    with pytest.raises(ValueError):
        AsyncPostgres(config, retries=0)


def test_retries_one_runs_operation_exactly_once():
    """COR-31: retries=1 → операция выполняется ровно один раз."""
    async def scenario():
        db = _make_db(retries=1)
        calls = {"n": 0}

        def sync_fn():
            calls["n"] += 1
            raise _ambiguous_conn_drop()

        with pytest.raises((psycopg2.OperationalError, RetryExhaustedError)):
            await db._run_with_retry_async(
                "fetch_all", sync_fn, read_only=True, idempotent=False,
            )
        assert calls["n"] == 1, "retries=1 must run the operation exactly once"

    asyncio.run(scenario())


def _minimal_config():
    """PostgresConfig с валидными полями (конструктор не должен дойти до
    реального коннекта — ValueError по retries срабатывает раньше)."""
    from shared.database.connection import PostgresConfig
    return PostgresConfig(
        host="localhost",
        port=5432,
        database="db",
        user="u",
        password="p",
    )
