"""Unit-тесты shared.database, затронутого relay-MQTT работой:

- is_unique_violation — детект SQLSTATE 23505 без утечки psycopg2 в caller-код
  (command repo ловит конкурентную relay_cmd_id-коллизию через него);
- fetch_all(read_only=...) — новый kwarg прокидывается в _fetch_all_sync, дефолт
  True сохраняет историческое read-only поведение (обратная совместимость для
  всех существующих fetch_all-вызовов во всех сервисах).

Живёт в locker_api-сьюте (а не shared_tests): shared_tests-образ не несёт
psycopg2 — он покрывает только psycopg2-free mqtt_client wrapper, тогда как
shared.database импортирует драйвер на уровне модуля.
"""
from __future__ import annotations

import asyncio

import psycopg2

from shared.database import is_foreign_key_violation, is_unique_violation
from shared.database.connection import AsyncPostgres

# --- is_unique_violation --------------------------------------------------------

def test_is_unique_violation_true_for_unique_violation_subclass():
    # psycopg2 на SQLSTATE 23505 поднимает именно этот подкласс.
    assert is_unique_violation(psycopg2.errors.UniqueViolation("dup")) is True


def test_is_unique_violation_false_for_other_integrity_error():
    # FK/NOT-NULL — тоже IntegrityError, но не UniqueViolation → False.
    assert is_unique_violation(psycopg2.errors.NotNullViolation("null")) is False
    assert is_unique_violation(psycopg2.errors.ForeignKeyViolation("fk")) is False


def test_is_unique_violation_true_for_bare_integrity_error_with_23505():
    # Fallback-ветка: не-UniqueViolation IntegrityError, несущий pgcode 23505
    # (например, переобёрнутый драйвером), всё равно детектится как unique.
    class _BareIntegrity(psycopg2.IntegrityError):
        pgcode = "23505"

    assert is_unique_violation(_BareIntegrity("dup")) is True


def test_is_unique_violation_false_for_bare_integrity_error_with_other_pgcode():
    class _FkIntegrity(psycopg2.IntegrityError):
        pgcode = "23503"

    assert is_unique_violation(_FkIntegrity("fk")) is False


def test_is_unique_violation_false_for_non_integrity_error():
    assert is_unique_violation(ValueError("nope")) is False
    assert is_unique_violation(psycopg2.OperationalError("conn lost")) is False


# --- is_foreign_key_violation --------------------------------------------------

def test_is_foreign_key_violation_true_for_fk_subclass():
    # psycopg2 на SQLSTATE 23503 поднимает именно этот подкласс.
    assert is_foreign_key_violation(psycopg2.errors.ForeignKeyViolation("fk")) is True


def test_is_foreign_key_violation_false_for_other_integrity_error():
    assert is_foreign_key_violation(psycopg2.errors.UniqueViolation("dup")) is False
    assert is_foreign_key_violation(psycopg2.errors.NotNullViolation("null")) is False


def test_is_foreign_key_violation_true_for_bare_integrity_error_with_23503():
    class _BareIntegrity(psycopg2.IntegrityError):
        pgcode = "23503"

    assert is_foreign_key_violation(_BareIntegrity("fk")) is True


def test_is_foreign_key_violation_false_for_non_integrity_error():
    assert is_foreign_key_violation(ValueError("nope")) is False
    assert is_foreign_key_violation(psycopg2.OperationalError("conn lost")) is False


# --- fetch_all(read_only=...) threading ----------------------------------------

class _ReadOnlyProbe(AsyncPostgres):
    """AsyncPostgres без реального пула: перехватываем _fetch_all_sync, чтобы
    проверить проброс read_only из публичного fetch_all, не трогая postgres."""

    def __init__(self) -> None:
        # Не зовём super().__init__ — он строит ThreadedConnectionPool (коннект к БД).
        self.captured_read_only: bool | None = None
        self._closed = False

    async def _run_with_retry_async(  # type: ignore[override]
        self, operation_name, sync_fn, *args, read_only=False, idempotent=False,
    ):
        # read_only/idempotent — управляют ретраем (COR-10), в sync_fn не идут.
        # Зовём sync-функцию напрямую (без executor/пула).
        return sync_fn(*args)

    def _fetch_all_sync(self, query, params, read_only):  # type: ignore[override]
        self.captured_read_only = read_only
        return [{"ok": 1}]


def test_fetch_all_defaults_to_read_only_true():
    """Обратная совместимость: дефолт read_only=True — поведение всех
    существующих fetch_all-вызовов не меняется."""
    async def scenario():
        db = _ReadOnlyProbe()
        rows = await db.fetch_all("SELECT 1")
        assert rows == [{"ok": 1}]
        assert db.captured_read_only is True
    asyncio.run(scenario())


def test_fetch_all_read_only_false_is_threaded_for_writes():
    """fetch_all(read_only=False) пробрасывает write-side флаг для UPDATE … RETURNING."""
    async def scenario():
        db = _ReadOnlyProbe()
        rows = await db.fetch_all(
            "UPDATE t SET x = 1 WHERE y = %s RETURNING id", (5,), read_only=False,
        )
        assert rows == [{"ok": 1}]
        assert db.captured_read_only is False
    asyncio.run(scenario())
