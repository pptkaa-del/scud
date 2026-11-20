"""Unit-тесты для resolve security-событий (COR-08).

Покрывает два инварианта:
1. Атрибуция: resolved_by берётся из аутентифицированного пользователя
   (current_user_id из X-Remote-User), а НЕ из тела запроса — иначе клиент
   может подделать, кто закрыл событие.
2. Идемпотентность: повторный resolve уже закрытого события возвращает 409,
   а не молча перезаписывает audit-trail. Несуществующее событие → 404.

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
import json
from typing import Any
import unittest
from unittest.mock import AsyncMock

import tornado.web

from app.handlers.security_events import (
    SecurityEventDetailHandler,
    SecurityEventListHandler,
)
from app.history_data.db.repositories.security_event_repository import (
    ResolveOutcome,
    SecurityEventRepository,
)
from tests.unit.conftest import _FakeRequest, make_handler


def _event_row(event_id: int, resolved_by: int) -> dict[str, Any]:
    return {
        "id": event_id,
        "locker_id": "11111111-1111-1111-1111-111111111111",
        "sensor_id": None,
        "event_type": "door_tamper",
        "severity": "critical",
        "violated_thresholds": [],
        "metadata": {},
        "occurred_at": datetime.fromisoformat("2026-07-10T08:00:00+00:00"),
        "resolved_at": datetime.fromisoformat("2026-07-10T09:00:00+00:00"),
        "resolved_by": resolved_by,
        "event_uid": None,
    }


def _capture(handler) -> dict:
    captured: dict[str, Any] = {"status": 200, "body": None}

    def fake_set_status(code: int, reason: str | None = None) -> None:
        captured["status"] = code
        handler._status_code = code

    def fake_set_header(name: str, value: str) -> None:
        pass

    def fake_write(chunk: Any) -> None:
        captured["body"] = chunk

    def fake_finish(chunk: Any = None) -> None:
        if chunk is not None:
            captured["body"] = chunk
        handler._finished = True

    handler.set_status = fake_set_status  # type: ignore[assignment]
    handler.set_header = fake_set_header  # type: ignore[assignment]
    handler.write = fake_write  # type: ignore[assignment]
    handler.finish = fake_finish  # type: ignore[assignment]
    return captured


def _make_detail_handler(
    body: bytes, user_id: int | None, db: Any
) -> SecurityEventDetailHandler:
    request = _FakeRequest(
        method="PATCH",
        uri="/api/v1/events/security/1",
        body=body,
        headers={"X-Remote-User": str(user_id)} if user_id is not None else {},
    )
    handler = make_handler(
        SecurityEventDetailHandler, settings={}, request=request, db=db
    )
    handler._current_user_id = user_id
    return handler


# ---------------------------------------------------------------------------
# Репозиторий: идемпотентный UPDATE с guard'ом resolved_at IS NULL
# ---------------------------------------------------------------------------


class TestResolveEventRepository(unittest.TestCase):
    def test_update_guards_on_resolved_at_is_null(self):
        """UPDATE ставит resolved_by из аргумента и закрывает только
        ещё-не-закрытые события (resolved_at IS NULL)."""
        captured: dict[str, Any] = {}

        async def run() -> None:
            db = AsyncMock()

            async def fetch_one(query, params, read_only=True):
                captured["query"] = query
                captured["params"] = params
                return _event_row(1, resolved_by=42)

            db.fetch_one = AsyncMock(side_effect=fetch_one)
            repo = SecurityEventRepository(db)
            outcome, event = await repo.resolve_event(event_id=1, resolved_by=42)
            captured["outcome"] = outcome
            captured["event"] = event

        asyncio.run(run())
        sql = captured["query"]
        self.assertIn("resolved_at IS NULL", sql)
        self.assertIn("resolved_by = %s", sql)
        self.assertEqual(captured["outcome"], ResolveOutcome.RESOLVED)
        self.assertEqual(captured["params"][0], 42)

    def test_already_resolved_returns_conflict_outcome(self):
        """0 строк обновлено + событие существует → ALREADY_RESOLVED."""
        async def run() -> ResolveOutcome:
            db = AsyncMock()
            # UPDATE ... RETURNING вернул None (0 строк), но SELECT по id находит.
            db.fetch_one = AsyncMock(
                side_effect=[None, {"id": 1}]
            )
            repo = SecurityEventRepository(db)
            outcome, _event = await repo.resolve_event(event_id=1, resolved_by=42)
            return outcome

        outcome = asyncio.run(run())
        self.assertEqual(outcome, ResolveOutcome.ALREADY_RESOLVED)

    def test_missing_event_returns_not_found_outcome(self):
        """0 строк обновлено + событие отсутствует → NOT_FOUND."""
        async def run() -> ResolveOutcome:
            db = AsyncMock()
            db.fetch_one = AsyncMock(side_effect=[None, None])
            repo = SecurityEventRepository(db)
            outcome, _event = await repo.resolve_event(event_id=999, resolved_by=42)
            return outcome

        outcome = asyncio.run(run())
        self.assertEqual(outcome, ResolveOutcome.NOT_FOUND)


# ---------------------------------------------------------------------------
# Handler: атрибуция из аутентифицированного пользователя + 409
# ---------------------------------------------------------------------------


class TestResolveEventHandler(unittest.TestCase):
    def test_resolved_by_taken_from_authenticated_user_not_body(self):
        """resolved_by приходит из current_user_id, а не из тела запроса."""
        captured_resolved_by: dict[str, Any] = {}

        async def run() -> dict:
            db = AsyncMock()

            async def resolve_event(event_id, resolved_by):
                captured_resolved_by["value"] = resolved_by
                return ResolveOutcome.RESOLVED, _event_row(event_id, resolved_by)

            # Тело пытается подделать resolved_by=999; должно игнорироваться.
            body = json.dumps({"resolved_by": 999}).encode("utf-8")
            handler = _make_detail_handler(body=body, user_id=7, db=db)
            cap = _capture(handler)

            # Патчим repo.resolve_event через monkeypatch на класс репозитория.
            import app.handlers.security_events as mod

            class _Repo:
                def __init__(self, _db): ...
                async def resolve_event(self, event_id, resolved_by):
                    return await resolve_event(event_id, resolved_by)

            orig = mod.SecurityEventRepository
            mod.SecurityEventRepository = _Repo  # type: ignore[assignment]
            try:
                await handler.patch("1")
            finally:
                mod.SecurityEventRepository = orig  # type: ignore[assignment]
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        # Аутентифицированный пользователь 7, НЕ 999 из тела.
        self.assertEqual(captured_resolved_by["value"], 7)

    def test_second_resolve_returns_409(self):
        """Повторный resolve уже закрытого события → 409 Conflict."""
        async def run() -> dict:
            db = AsyncMock()
            handler = _make_detail_handler(body=b"{}", user_id=7, db=db)
            cap = _capture(handler)

            import app.handlers.security_events as mod

            class _Repo:
                def __init__(self, _db): ...
                async def resolve_event(self, event_id, resolved_by):
                    return ResolveOutcome.ALREADY_RESOLVED, None

            orig = mod.SecurityEventRepository
            mod.SecurityEventRepository = _Repo  # type: ignore[assignment]
            try:
                await handler.patch("1")
            finally:
                mod.SecurityEventRepository = orig  # type: ignore[assignment]
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 409)

    def test_resolve_missing_event_returns_404(self):
        async def run() -> dict:
            db = AsyncMock()
            handler = _make_detail_handler(body=b"{}", user_id=7, db=db)
            cap = _capture(handler)

            import app.handlers.security_events as mod

            class _Repo:
                def __init__(self, _db): ...
                async def resolve_event(self, event_id, resolved_by):
                    return ResolveOutcome.NOT_FOUND, None

            orig = mod.SecurityEventRepository
            mod.SecurityEventRepository = _Repo  # type: ignore[assignment]
            try:
                await handler.patch("999")
            finally:
                mod.SecurityEventRepository = orig  # type: ignore[assignment]
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 404)


# ---------------------------------------------------------------------------
# VAL-01: невалидные query-фильтры списка → 400, а не 500
# ---------------------------------------------------------------------------


def _make_list_handler(query_arguments: dict, db: Any) -> SecurityEventListHandler:
    request = _FakeRequest(
        method="GET",
        uri="/api/v1/events/security",
        headers={"X-Remote-User": "7"},
        query_arguments=query_arguments,
    )
    handler = make_handler(
        SecurityEventListHandler, settings={}, request=request, db=db
    )
    handler._current_user_id = 7
    return handler


class TestSecurityEventListFilterValidation(unittest.TestCase):
    """VAL-01: невалидные enum/uuid в query-фильтрах списка событий.

    Раньше bogus severity/event_type/locker_id уходили в WHERE по enum/uuid-
    колонке, psycopg2 поднимал 22P02, broad-except в GET маскировал это под
    500. Теперь фильтры валидируются ДО SQL → 400.
    """

    def _run_get(self, query_arguments: dict, db: Any = None) -> dict:
        async def run() -> dict:
            nonlocal db
            if db is None:
                db = AsyncMock()
                db.fetch_all = AsyncMock(return_value=[])
                db.fetch_one = AsyncMock(return_value={"count": 0})
            handler = _make_list_handler(query_arguments, db)
            cap = _capture(handler)
            with contextlib.suppress(tornado.web.Finish, tornado.web.HTTPError):
                await handler.get()
            return cap

        return asyncio.run(run())

    @staticmethod
    def _raising_db() -> AsyncMock:
        """БД, бросающая 22P02 как реальный Postgres на enum/uuid-mismatch.

        На старом коде bogus-фильтр доходил до этого вызова → broad except →
        write_internal_error (500). С валидацией до SQL мок не вызывается.
        """
        import psycopg2.errors
        db = AsyncMock()
        db.fetch_all = AsyncMock(
            side_effect=psycopg2.errors.InvalidTextRepresentation("22P02")
        )
        db.fetch_one = AsyncMock(
            side_effect=psycopg2.errors.InvalidTextRepresentation("22P02")
        )
        return db

    def test_bogus_severity_filter_returns_400(self):
        cap = self._run_get({"severity": [b"super-critical"]}, db=self._raising_db())
        self.assertEqual(cap["status"], 400)

    def test_bogus_event_type_filter_returns_400(self):
        cap = self._run_get({"event_type": [b"not-a-type"]}, db=self._raising_db())
        self.assertEqual(cap["status"], 400)

    def test_non_uuid_locker_id_filter_returns_400(self):
        cap = self._run_get(
            {"locker_id": [b"not-a-uuid"]}, db=self._raising_db()
        )
        self.assertEqual(cap["status"], 400)

    def test_non_integer_limit_returns_400(self):
        cap = self._run_get({"limit": [b"abc"]})
        self.assertEqual(cap["status"], 400)

    def test_valid_severity_filter_passes(self):
        cap = self._run_get({"severity": [b"critical"]})
        self.assertEqual(cap["status"], 200)

    def test_command_late_execution_filter_passes(self):
        cap = self._run_get({"event_type": [b"command_late_execution"]})
        self.assertEqual(cap["status"], 200)


class TestSecurityEventCreateStrictBody(unittest.TestCase):
    """VAL-03 + ревью: тело с неизвестным ключом → 400 БЕЗ ложного ERROR-лога.

    get_json_body_as поднимает Finish (400 уже записан). На старом коде
    broad-except в post() ловил Finish и звал logger.error → шум в алертинге
    (клиент всё равно получал 400, т.к. _finished подавляет повторную запись).
    Тест ловит именно проброс Finish: error НЕ логируется.
    """

    def _post_unknown_field(self) -> tuple[dict, list]:
        body = json.dumps({
            "locker_id": "11111111-1111-1111-1111-111111111111",
            "event_type": "intrusion",
            "severity": "high",
            "totally_unknown_field": "x",
        }).encode("utf-8")
        request = _FakeRequest(
            method="POST", uri="/api/v1/events/security", body=body,
            headers={"X-Remote-User": "7"},
        )
        db = AsyncMock()
        db.fetch_one_strict = AsyncMock(
            side_effect=AssertionError("БД не должна вызываться при 400")
        )
        handler = make_handler(
            SecurityEventListHandler, settings={}, request=request, db=db
        )
        handler._current_user_id = 7
        cap = _capture(handler)

        import app.handlers.security_events as mod
        errors: list = []

        async def run() -> None:
            original = mod.logger.error
            mod.logger.error = lambda *a, **k: errors.append((a, k))  # type: ignore[assignment]
            try:
                with contextlib.suppress(tornado.web.Finish, tornado.web.HTTPError):
                    await handler.post()
            finally:
                mod.logger.error = original  # type: ignore[assignment]

        asyncio.run(run())
        return cap, errors

    def test_unknown_field_returns_400_without_error_log(self):
        cap, errors = self._post_unknown_field()
        self.assertEqual(cap["status"], 400)
        self.assertEqual(
            errors, [], "strict-400 не должен логироваться как ERROR (Finish проброшен)"
        )


if __name__ == "__main__":
    unittest.main()
