"""Unit-тесты чистого модуля shared.database.retry.

Покрывают should_retry (truth-table COR-10), is_connection_broken (гигиена пула),
record_db_metric (no-op-safe) и тот факт, что AsyncPostgres._should_retry —
тонкий делегат к retry.should_retry (через __new__-инстанс, без реального пула).

Живёт в locker_api-сьюте: shared_tests-образ не несёт psycopg2, тогда как
shared.database.retry импортирует драйвер на уровне модуля.
"""
from __future__ import annotations

import psycopg2

from shared.database import retry
from shared.database.connection import AsyncPostgres
from shared.database.retry import (
    RETRY_SQLSTATES,
    is_connection_broken,
    record_db_metric,
    should_retry,
)


def _ambiguous_conn_drop() -> psycopg2.OperationalError:
    """Неоднозначный обрыв: OperationalError без SQLSTATE (pgcode None)."""
    exc = psycopg2.OperationalError("server closed the connection unexpectedly")
    assert getattr(exc, "pgcode", None) is None
    return exc


def _retryable_sqlstate_error() -> psycopg2.OperationalError:
    """OperationalError с retryable SQLSTATE (admin_shutdown 57P01)."""
    class _AdminShutdown(psycopg2.OperationalError):
        pgcode = "57P01"

    exc = _AdminShutdown("admin shutdown")
    assert exc.pgcode in RETRY_SQLSTATES
    return exc


def _non_retryable_pgcode_error() -> psycopg2.ProgrammingError:
    """ProgrammingError с не-retryable SQLSTATE (syntax_error 42601)."""
    class _SyntaxError(psycopg2.ProgrammingError):
        pgcode = "42601"

    return _SyntaxError("syntax error")


# --- should_retry truth table --------------------------------------------------

def test_should_retry_ambiguous_read_only_true() -> None:
    assert should_retry(_ambiguous_conn_drop(), read_only=True, idempotent=False) is True


def test_should_retry_ambiguous_write_non_idempotent_false() -> None:
    assert should_retry(_ambiguous_conn_drop(), read_only=False, idempotent=False) is False


def test_should_retry_ambiguous_write_idempotent_true() -> None:
    assert should_retry(_ambiguous_conn_drop(), read_only=False, idempotent=True) is True


def test_should_retry_retryable_sqlstate_true_regardless_of_mode() -> None:
    # Явный retryable SQLSTATE → True независимо от read/write/idempotent.
    assert should_retry(_retryable_sqlstate_error(), read_only=False, idempotent=False) is True
    assert should_retry(_retryable_sqlstate_error(), read_only=True, idempotent=False) is True


def test_should_retry_non_retryable_pgcode_false() -> None:
    assert should_retry(_non_retryable_pgcode_error(), read_only=True, idempotent=True) is False


# --- is_connection_broken ------------------------------------------------------

def test_is_connection_broken_true_for_retryable_sqlstate() -> None:
    assert is_connection_broken(_retryable_sqlstate_error()) is True


def test_is_connection_broken_true_for_operational_error_without_pgcode() -> None:
    assert is_connection_broken(_ambiguous_conn_drop()) is True


def test_is_connection_broken_true_for_interface_error_without_pgcode() -> None:
    exc = psycopg2.InterfaceError("connection already closed")
    assert getattr(exc, "pgcode", None) is None
    assert is_connection_broken(exc) is True


def test_is_connection_broken_false_for_programming_error_without_pgcode() -> None:
    exc = psycopg2.ProgrammingError("no results to fetch")
    assert getattr(exc, "pgcode", None) is None
    assert is_connection_broken(exc) is False


# --- record_db_metric ----------------------------------------------------------

def test_record_db_metric_is_no_op_safe() -> None:
    # Не должно кидать ни при наличии, ни при отсутствии prometheus_client.
    record_db_metric("smoke_op", 0.0)
    record_db_metric("smoke_op", 1.5)


# --- AsyncPostgres._should_retry делегирует к retry.should_retry ----------------

def test_async_postgres_should_retry_delegates_matching_results() -> None:
    db = AsyncPostgres.__new__(AsyncPostgres)
    cases = [
        (_ambiguous_conn_drop(), True, False),
        (_ambiguous_conn_drop(), False, False),
        (_ambiguous_conn_drop(), False, True),
        (_retryable_sqlstate_error(), False, False),
        (_non_retryable_pgcode_error(), True, True),
    ]
    for exc, read_only, idempotent in cases:
        method_result = db._should_retry(exc, read_only=read_only, idempotent=idempotent)
        pure_result = retry.should_retry(exc, read_only=read_only, idempotent=idempotent)
        assert method_result is pure_result
