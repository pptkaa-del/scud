"""Unit-тесты для CommandHistoryRepository (выборка/подсчёт).

Проверяют, что новые фильтры (executed_from/executed_to) и порядок (order)
корректно строят SQL — без интерполяции пользовательского ввода: order
маппится в фиксированный ORDER BY-фрагмент из allowlist. Существующее
поведение по умолчанию (created_at DESC) остаётся неизменным.

Мокаем db.fetch_all/db.fetch_one и инспектируем перехваченный SQL + параметры.
Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import unittest
from unittest.mock import AsyncMock

from app.history_data.db.repositories.command_history_repository import (
    CommandHistoryOrder,
    CommandHistoryRepository,
)

_EXECUTED_FROM = datetime.fromisoformat("2026-07-10T00:00:00+00:00")
_EXECUTED_TO = datetime.fromisoformat("2026-07-10T23:59:59+00:00")


def _db() -> AsyncMock:
    db = AsyncMock()
    db.fetch_all = AsyncMock(return_value=[])
    db.fetch_one = AsyncMock(return_value={"count": 0})
    return db


# ---------------------------------------------------------------------------
# COR-07: upsert не должен регрессировать статус (executed -> sent)
# ---------------------------------------------------------------------------


class TestUpsertStatusMonotonic(unittest.TestCase):
    def test_upsert_status_uses_rank_guard(self):
        """ON CONFLICT не затирает статус безусловно — статус продвигается
        только вперёд по рангу (pending<sent<delivered<executed/failed).
        """
        captured = {}

        def _row_for(status: str) -> dict:
            return {
                "id": 1,
                "command_id": "cmd-1",
                "locker_id": "loc-1",
                "locker_inventory_number": None,
                "command_type": "open",
                "parameters": {},
                "created_by": None,
                "user_phone": None,
                "user_full_name": None,
                "created_at": datetime.now(),
                "sent_at": None,
                "executed_at": None,
                "failed_at": None,
                "status": status,
                "result": {},
                "error_message": None,
            }

        async def run() -> None:
            db = AsyncMock()

            async def fetch_one_strict(query, params):
                captured["query"] = query
                return _row_for(params[8])  # status — 9-й параметр

            db.fetch_one_strict = AsyncMock(side_effect=fetch_one_strict)
            repo = CommandHistoryRepository(db)
            await repo.upsert(
                command_id="cmd-1", locker_id="loc-1",
                command_type="open", status="executed",
            )

        asyncio.run(run())
        sql = captured["query"]
        # Безусловной перезаписи статуса быть не должно.
        self.assertNotIn("status = EXCLUDED.status", sql)
        # Должна быть rank-guard логика на статусе.
        self.assertIn("status = CASE", sql)

    def test_upsert_freezes_terminal_fields_on_terminal_conflict(self):
        """failed→executed (поздний rep после TTL) не протекает в executed_at,
        failed_at, result и error_message сохранённой записи."""
        captured = {}

        async def run() -> None:
            db = AsyncMock()

            async def fetch_one_strict(query, params):
                captured["query"] = query
                raise RuntimeError("stop")

            db.fetch_one_strict = AsyncMock(side_effect=fetch_one_strict)
            with self.assertRaises(RuntimeError):
                await CommandHistoryRepository(db).upsert(
                    command_id="cmd-1", locker_id="loc-1",
                    command_type="open", status="executed",
                )

        asyncio.run(run())
        sql = " ".join(captured["query"].split())
        conflict = (
            "(command_history.status IN ('executed', 'failed') "
            "AND EXCLUDED.status IN ('executed', 'failed') "
            "AND EXCLUDED.status <> command_history.status)"
        )
        self.assertIn(f"executed_at = CASE WHEN {conflict} THEN command_history.executed_at", sql)
        self.assertIn(f"failed_at = CASE WHEN {conflict} THEN command_history.failed_at", sql)
        self.assertIn(f"result = CASE WHEN {conflict} THEN command_history.result", sql)
        self.assertIn(
            f"error_message = CASE WHEN {conflict} THEN command_history.error_message", sql,
        )


class TestGetByFilters(unittest.TestCase):
    def test_default_order_created_desc_no_executed_filter(self):
        async def run() -> AsyncMock:
            db = _db()
            await CommandHistoryRepository(db).get_by_filters()
            return db

        db = asyncio.run(run())
        sql = db.fetch_all.call_args[0][0]
        self.assertIn("ORDER BY created_at DESC", sql)
        self.assertNotIn("executed_at", sql)

    def test_executed_range_adds_clauses_and_params(self):
        async def run() -> AsyncMock:
            db = _db()
            await CommandHistoryRepository(db).get_by_filters(
                executed_from=_EXECUTED_FROM, executed_to=_EXECUTED_TO
            )
            return db

        db = asyncio.run(run())
        sql, params = db.fetch_all.call_args[0]
        self.assertIn("executed_at >= %s", sql)
        self.assertIn("executed_at <= %s", sql)
        self.assertIn(_EXECUTED_FROM, params)
        self.assertIn(_EXECUTED_TO, params)

    def test_order_executed_asc_fragment(self):
        async def run() -> AsyncMock:
            db = _db()
            await CommandHistoryRepository(db).get_by_filters(
                order=CommandHistoryOrder.EXECUTED_ASC
            )
            return db

        db = asyncio.run(run())
        sql = db.fetch_all.call_args[0][0]
        self.assertIn("ORDER BY executed_at ASC", sql)
        self.assertNotIn("created_at DESC", sql)

    def test_order_created_desc_fragment(self):
        async def run() -> AsyncMock:
            db = _db()
            await CommandHistoryRepository(db).get_by_filters(
                order=CommandHistoryOrder.CREATED_DESC
            )
            return db

        db = asyncio.run(run())
        sql = db.fetch_all.call_args[0][0]
        self.assertIn("ORDER BY created_at DESC", sql)

    def test_only_two_allowlisted_order_fragments(self):
        """allowlist содержит ровно два фрагмента; пользовательский ввод не попадает в SQL."""
        from app.history_data.db.repositories.command_history_repository import _ORDER_BY_SQL
        self.assertEqual(
            set(_ORDER_BY_SQL.values()), {"created_at DESC", "executed_at ASC"}
        )


class TestGetCount(unittest.TestCase):
    def test_executed_range_adds_clauses_and_params(self):
        async def run() -> AsyncMock:
            db = _db()
            await CommandHistoryRepository(db).get_count(
                executed_from=_EXECUTED_FROM, executed_to=_EXECUTED_TO
            )
            return db

        db = asyncio.run(run())
        sql, params = db.fetch_one.call_args[0]
        self.assertIn("executed_at >= %s", sql)
        self.assertIn("executed_at <= %s", sql)
        self.assertIn(_EXECUTED_FROM, params)
        self.assertIn(_EXECUTED_TO, params)

    def test_no_executed_filter_when_absent(self):
        async def run() -> AsyncMock:
            db = _db()
            await CommandHistoryRepository(db).get_count()
            return db

        db = asyncio.run(run())
        sql = db.fetch_one.call_args[0][0]
        self.assertNotIn("executed_at", sql)


if __name__ == "__main__":
    unittest.main()
