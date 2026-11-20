"""Unit-тесты для internal command-history handler (handlers/internal_command_history.py).

Покрывает `InternalCommandHistoryHandler.get` (GET /internal/api/v1/commands/history):
happy-path (200 + полные строки + total), фильтрацию (command_type/status/диапазон
по executed_at), выбор порядка (created_at_desc / executed_at_asc → разный ORDER BY,
проверяем через перехват SQL в моке repo.get_by_filters), кэп limit и валидацию
query-параметров (400 при битой дате/limit/order).

Handler наследуется от JSONHandler и НЕ требует X-Remote-User (закрыт Apache
снаружи). Мокаем db.fetch_all/db.fetch_one; статус и тело ответа перехватываем
через patch на set_status/write.

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from typing import Any
import unittest
from unittest.mock import AsyncMock

from app.handlers.internal_command_history import InternalCommandHistoryHandler
from app.history_data.db.repositories.command_history_repository import CommandHistoryOrder
from tests.unit.conftest import _FakeRequest, make_handler

_EXECUTED_FROM = "2026-07-10T00:00:00+00:00"
_EXECUTED_TO = "2026-07-10T23:59:59+00:00"

# Полные строки command_history (как из SELECT *), достаточные для dacite.from_dict.
_ROWS: list[dict[str, Any]] = [
    {
        "id": 1,
        "command_id": "11111111-1111-1111-1111-111111111111",
        "locker_id": "22222222-2222-2222-2222-222222222222",
        "locker_inventory_number": "СО №100500",
        "command_type": "open",
        "parameters": {},
        "created_by": 7,
        "user_phone": "+79991234567",
        "user_full_name": "Иванов И.И.",
        "created_at": datetime.fromisoformat("2026-07-10T07:55:00+00:00"),
        "sent_at": datetime.fromisoformat("2026-07-10T07:56:00+00:00"),
        "executed_at": datetime.fromisoformat("2026-07-10T08:00:00+00:00"),
        "failed_at": None,
        "status": "executed",
        "result": {"door": "opened"},
        "error_message": None,
    },
    {
        "id": 2,
        "command_id": "33333333-3333-3333-3333-333333333333",
        "locker_id": "22222222-2222-2222-2222-222222222222",
        "locker_inventory_number": "СО №100501",
        "command_type": "open",
        "parameters": {},
        "created_by": 8,
        "user_phone": "+79991234568",
        "user_full_name": "Петров П.П.",
        "created_at": datetime.fromisoformat("2026-07-10T09:25:00+00:00"),
        "sent_at": datetime.fromisoformat("2026-07-10T09:28:00+00:00"),
        "executed_at": datetime.fromisoformat("2026-07-10T09:30:00+00:00"),
        "failed_at": None,
        "status": "executed",
        "result": {},
        "error_message": None,
    },
]


def _capture(handler) -> dict:
    """Перехват set_status/write для проверки результата."""
    captured: dict[str, Any] = {"status": 200, "body": None}

    def fake_set_status(code: int, reason: str | None = None) -> None:
        captured["status"] = code
        handler._status_code = code

    def fake_set_header(name: str, value: str) -> None:
        pass

    def fake_write(chunk: Any) -> None:
        captured["body"] = chunk

    handler.set_status = fake_set_status  # type: ignore[assignment]
    handler.set_header = fake_set_header  # type: ignore[assignment]
    handler.write = fake_write  # type: ignore[assignment]
    return captured


def _make_handler(query: dict | None = None, db: Any = None) -> InternalCommandHistoryHandler:
    qa: dict = {}
    if query:
        for k, v in query.items():
            qa[k] = [v.encode("utf-8") if isinstance(v, str) else v]
    request = _FakeRequest(
        method="GET",
        uri="/internal/api/v1/commands/history",
        query_arguments=qa,
    )
    return make_handler(
        InternalCommandHistoryHandler,
        settings={},
        request=request,
        db=db if db is not None else object(),
    )


def _db_returning(rows: list[dict[str, Any]], total: int) -> AsyncMock:
    db = AsyncMock()
    db.fetch_all = AsyncMock(return_value=rows)
    db.fetch_one = AsyncMock(return_value={"count": total})
    return db


class TestInternalCommandHistoryGet(unittest.TestCase):
    """GET /internal/api/v1/commands/history."""

    def test_happy_path_returns_full_rows_and_total(self):
        async def run() -> dict:
            db = _db_returning(_ROWS, 2)
            handler = _make_handler(
                query={
                    "command_type": "open",
                    "status": "executed",
                    "executed_from": _EXECUTED_FROM,
                    "executed_to": _EXECUTED_TO,
                },
                db=db,
            )
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        body = json.loads(cap["body"])
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["limit"], 1000)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(len(body["items"]), 2)
        # Полные строки, а не 4-полевая проекция.
        first = body["items"][0]
        for field in (
            "id", "command_id", "locker_id", "locker_inventory_number",
            "command_type", "parameters", "created_by", "user_phone",
            "user_full_name", "created_at", "sent_at", "executed_at",
            "failed_at", "status", "result", "error_message",
        ):
            self.assertIn(field, first)
        self.assertEqual(first["command_type"], "open")
        self.assertEqual(first["status"], "executed")
        # datetime сериализован parse-able через fromisoformat.
        datetime.fromisoformat(first["executed_at"])

    def test_filters_passed_to_repo(self):
        """command_type/status/executed-диапазон попадают в SQL + параметры."""
        async def run() -> AsyncMock:
            db = _db_returning(_ROWS, 2)
            handler = _make_handler(
                query={
                    "command_type": "open",
                    "status": "executed",
                    "executed_from": _EXECUTED_FROM,
                    "executed_to": _EXECUTED_TO,
                },
                db=db,
            )
            _capture(handler)
            await handler.get()
            return db

        db = asyncio.run(run())
        sql, params = db.fetch_all.call_args[0]
        self.assertIn("command_type = %s", sql)
        self.assertIn("status = %s", sql)
        self.assertIn("executed_at >= %s", sql)
        self.assertIn("executed_at <= %s", sql)
        self.assertIn("open", params)
        self.assertIn("executed", params)

    def test_order_executed_asc_builds_executed_order_by(self):
        async def run() -> AsyncMock:
            db = _db_returning(_ROWS, 2)
            handler = _make_handler(
                query={"order": CommandHistoryOrder.EXECUTED_ASC.value}, db=db
            )
            _capture(handler)
            await handler.get()
            return db

        db = asyncio.run(run())
        sql = db.fetch_all.call_args[0][0]
        self.assertIn("ORDER BY executed_at ASC", sql)
        self.assertNotIn("created_at DESC", sql)

    def test_default_order_is_created_desc(self):
        async def run() -> AsyncMock:
            db = _db_returning(_ROWS, 2)
            handler = _make_handler(query={}, db=db)
            _capture(handler)
            await handler.get()
            return db

        db = asyncio.run(run())
        sql = db.fetch_all.call_args[0][0]
        self.assertIn("ORDER BY created_at DESC", sql)

    def test_limit_capped_and_offset_echoed(self):
        async def run() -> dict:
            db = _db_returning([], 0)
            handler = _make_handler(
                query={"limit": "99999", "offset": "20"}, db=db
            )
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        body = json.loads(cap["body"])
        self.assertEqual(body["limit"], 5000)
        self.assertEqual(body["offset"], 20)

    def test_bad_executed_from_returns_400(self):
        async def run() -> dict:
            db = AsyncMock()
            db.fetch_all = AsyncMock(side_effect=AssertionError("DB не должен вызываться"))
            handler = _make_handler(query={"executed_from": "not-a-date"}, db=db)
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)

    def test_bad_executed_to_returns_400(self):
        async def run() -> dict:
            handler = _make_handler(query={"executed_to": "garbage"})
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)

    def test_bad_order_returns_400(self):
        async def run() -> dict:
            db = AsyncMock()
            db.fetch_all = AsyncMock(side_effect=AssertionError("DB не должен вызываться"))
            handler = _make_handler(query={"order": "totally_bogus"}, db=db)
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)

    def test_non_integer_limit_returns_400(self):
        async def run() -> dict:
            handler = _make_handler(query={"limit": "abc"})
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)


if __name__ == "__main__":
    unittest.main()
