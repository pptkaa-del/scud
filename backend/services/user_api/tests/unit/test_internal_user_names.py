"""Unit-тесты для InternalUserNamesHandler (GET /internal/users/names).

Batch-resolve ФИО по списку id. Handler наследуется от JSONHandler и НЕ
требует X-Remote-User (закрыт Apache снаружи). Мокаем
UserRepository.get_full_names_by_ids, статус/тело перехватываем через
patch на методах handler'а (см. test_internal_auth.py).

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).
"""
from __future__ import annotations

import asyncio
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from app.handlers.internal_auth import InternalUserNamesHandler
from tests.unit.conftest import _FakeRequest, make_handler


def _capture(handler) -> dict:
    """Перехват set_status/finish/write_json (как в test_internal_auth)."""
    captured: dict[str, Any] = {"status": None, "finished": False, "body": None}

    def fake_set_status(code: int, reason: str | None = None) -> None:
        captured["status"] = code
        handler._status_code = code

    def fake_finish(chunk: Any = None) -> None:
        captured["finished"] = True
        if chunk is not None:
            captured["body"] = chunk

    def fake_write_json(data: Any, status_code: int = 200) -> None:
        captured["status"] = status_code
        captured["body"] = data
        handler._status_code = status_code

    handler.set_status = fake_set_status  # type: ignore[assignment]
    handler.finish = fake_finish  # type: ignore[assignment]
    handler.write_json = fake_write_json  # type: ignore[assignment]
    return captured


def _make_names_handler(ids: str | None = None) -> InternalUserNamesHandler:
    qa: dict = {}
    if ids is not None:
        qa["ids"] = [ids.encode("utf-8")]
    request = _FakeRequest(
        method="GET",
        uri="/internal/users/names",
        query_arguments=qa,
    )
    return make_handler(InternalUserNamesHandler, settings={}, request=request, db=object())


class TestInternalUserNames(unittest.TestCase):
    """GET /internal/users/names?ids=... → {"names": {...}} или 400."""

    def test_happy_path_omits_missing_ids(self):
        """ids=1,2,3 — repo вернул 1 и 3, id 2 отсутствует → опущен из map."""
        async def run() -> dict:
            handler = _make_names_handler(ids="1,2,3")
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_full_names_by_ids",
                new=AsyncMock(return_value={1: "Иванов", 3: "Петров"}),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(cap["body"], {"names": {"1": "Иванов", "3": "Петров"}})

    def test_passes_deduped_ids_to_repo(self):
        """Дубликаты схлопываются перед вызовом repo, порядок сохраняется."""
        captured_ids: dict[str, Any] = {}

        async def fake_repo(_self: Any, ids: list[int]) -> dict[int, str]:
            captured_ids["ids"] = ids
            return {}

        async def run() -> dict:
            handler = _make_names_handler(ids="3,1,3,1,2")
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_full_names_by_ids",
                new=fake_repo,
            ):
                await handler.get()
            return cap

        asyncio.run(run())
        self.assertEqual(captured_ids["ids"], [3, 1, 2])

    def test_empty_ids_returns_200_empty_map(self):
        async def run() -> dict:
            handler = _make_names_handler(ids="")
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(cap["body"], {"names": {}})

    def test_missing_ids_arg_returns_200_empty_map(self):
        async def run() -> dict:
            handler = _make_names_handler(ids=None)
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(cap["body"], {"names": {}})

    def test_malformed_ids_returns_400(self):
        async def run() -> dict:
            handler = _make_names_handler(ids="a,b")
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)
        self.assertTrue(cap["finished"])

    def test_too_many_ids_returns_400(self):
        async def run() -> dict:
            # 1001 уникальных id — превышает лимит 1000.
            raw = ",".join(str(n) for n in range(1, 1002))
            handler = _make_names_handler(ids=raw)
            cap = _capture(handler)
            # Repo не должен вызываться — отсекаем до обращения к БД.
            with patch(
                "app.handlers.internal_auth.UserRepository.get_full_names_by_ids",
                new=AsyncMock(side_effect=AssertionError("repo must not be called")),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)
        self.assertTrue(cap["finished"])


if __name__ == "__main__":
    unittest.main()
