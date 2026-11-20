"""Unit-тесты чистого модуля shared.database.helpers (SQLSTATE-детекторы).

Зеркалят покрытие is_unique_violation/is_foreign_key_violation, но импортируют
функции напрямую из submodule (а не через фасад), фиксируя, что после декомпозиции
connection.py логика живёт в helpers.py.

Живёт в locker_api-сьюте (а не shared_tests): shared_tests-образ не несёт
psycopg2, тогда как shared.database.helpers импортирует драйвер на уровне модуля.
"""
from __future__ import annotations

import psycopg2

from shared.database.helpers import is_foreign_key_violation, is_unique_violation

# --- is_unique_violation --------------------------------------------------------

def test_is_unique_violation_true_for_unique_violation_subclass() -> None:
    assert is_unique_violation(psycopg2.errors.UniqueViolation("dup")) is True


def test_is_unique_violation_false_for_foreign_key_violation() -> None:
    assert is_unique_violation(psycopg2.errors.ForeignKeyViolation("fk")) is False


def test_is_unique_violation_false_for_not_null_violation() -> None:
    assert is_unique_violation(psycopg2.errors.NotNullViolation("null")) is False


def test_is_unique_violation_false_for_non_integrity_error() -> None:
    assert is_unique_violation(ValueError("nope")) is False
    assert is_unique_violation(psycopg2.OperationalError("conn lost")) is False


def test_is_unique_violation_true_for_bare_integrity_error_with_23505() -> None:
    # Fallback-ветка: общий IntegrityError, несущий pgcode 23505, детектится.
    class _BareIntegrity(psycopg2.IntegrityError):
        pgcode = "23505"

    assert is_unique_violation(_BareIntegrity("dup")) is True


def test_is_unique_violation_false_for_bare_integrity_error_with_other_pgcode() -> None:
    class _FkIntegrity(psycopg2.IntegrityError):
        pgcode = "23503"

    assert is_unique_violation(_FkIntegrity("fk")) is False


# --- is_foreign_key_violation --------------------------------------------------

def test_is_foreign_key_violation_true_for_fk_subclass() -> None:
    assert is_foreign_key_violation(psycopg2.errors.ForeignKeyViolation("fk")) is True


def test_is_foreign_key_violation_false_for_unique_violation() -> None:
    assert is_foreign_key_violation(psycopg2.errors.UniqueViolation("dup")) is False


def test_is_foreign_key_violation_false_for_not_null_violation() -> None:
    assert is_foreign_key_violation(psycopg2.errors.NotNullViolation("null")) is False


def test_is_foreign_key_violation_false_for_non_integrity_error() -> None:
    assert is_foreign_key_violation(ValueError("nope")) is False
    assert is_foreign_key_violation(psycopg2.OperationalError("conn lost")) is False


def test_is_foreign_key_violation_true_for_bare_integrity_error_with_23503() -> None:
    class _BareIntegrity(psycopg2.IntegrityError):
        pgcode = "23503"

    assert is_foreign_key_violation(_BareIntegrity("fk")) is True


def test_is_foreign_key_violation_false_for_bare_integrity_error_with_other_pgcode() -> None:
    class _UniqueIntegrity(psycopg2.IntegrityError):
        pgcode = "23505"

    assert is_foreign_key_violation(_UniqueIntegrity("dup")) is False
