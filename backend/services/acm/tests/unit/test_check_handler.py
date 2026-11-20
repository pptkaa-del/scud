"""Unit-тесты для app.handlers.check.CheckHandler.

Покрывают 7-шаговый алгоритм CheckHandler.get():
- отсутствие X-Request-Path → 403
- публичный путь без токена → 200
- отсутствие X-Access-Token → 401
- невалидный токен (auth_api → None) → 401
- cache.get_or_load → None (пользователь не найден) → 401
- ctx.state != ACTIVE → invalidate + 403
- неизвестная роль → 403
- check_access False → 403 с reason
- worker с Допусками → 200 + X-Remote-Lockers="uuid1,uuid2"
- operator/administrator → 200 + X-Remote-Lockers="*"

Тесты следуют паттерну Astra Linux: unittest.TestCase + asyncio.run()
вместо pytest-asyncio. Handler конструируется в обход Tornado __init__ через
self-contained _FakeApp/_FakeRequest (см. backend/services/auth_api/tests/unit/conftest.py).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.cache import UserContext, UserContextCache
from app.handlers.check import CheckHandler
from app.rules import Role
from shared.enums import UserState
from shared.redis_client import RedisConnectionError

# ---------------------------------------------------------------------------
# Tornado stand-ins (копия из auth_api conftest, адаптированная под ACM check)
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.ui_methods: dict = {}
        self.ui_modules: dict = {}


class _FakeHeaders(dict):
    """dict-like с регистронезависимым `.get` (как HTTPHeaders)."""

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _FakeRequest:
    def __init__(
        self,
        headers: dict | None = None,
        method: str = "GET",
        uri: str = "/acm/check",
    ) -> None:
        self.headers = _FakeHeaders(headers or {})
        self.method = method
        self.uri = uri
        self.body = b""
        self.remote_ip = "127.0.0.1"
        self.cookies: dict = {}


def _make_handler(headers: dict | None = None, cache: Any = None) -> CheckHandler:
    """Сконструировать CheckHandler без Tornado __init__.

    Tornado RequestHandler.finish() обращается к self.request.connection,
    которого у нас нет — поэтому подменяем finish/flush на no-op,
    выставляющий _finished=True (этого достаточно для assert'ов в тестах).
    """
    settings = {"cache": cache, "auth_api_url": "http://auth_api:8003"}
    handler = CheckHandler.__new__(CheckHandler)
    handler.application = _FakeApp(settings)  # type: ignore[assignment]
    handler.request = _FakeRequest(headers=headers)  # type: ignore[assignment]
    handler.db = object()
    handler._headers = {}
    handler._write_buffer = []
    handler._status_code = 200
    handler._reason = "OK"
    handler._finished = False

    def _fake_finish(chunk=None):
        if chunk is not None:
            handler._write_buffer.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        handler._finished = True

    handler.finish = _fake_finish  # type: ignore[assignment]
    handler.flush = lambda include_footers=False: None  # type: ignore[assignment]
    return handler


def _make_cache(ctx: UserContext | None = None) -> MagicMock:
    """Мок UserContextCache: get_or_load возвращает заданный ctx, invalidate — AsyncMock."""
    cache = MagicMock()
    cache.get_or_load = AsyncMock(return_value=ctx)
    cache.invalidate = AsyncMock(return_value=None)
    return cache


def _problem_body(handler: CheckHandler) -> dict:
    """Извлечь тело Problem Details из write_buffer хэндлера."""
    raw = b"".join(handler._write_buffer).decode("utf-8")
    return json.loads(raw)


WORKER_LOCKER = "550e8400-e29b-41d4-a716-446655440000"
OTHER_LOCKER = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


def _ctx(
    role: str = "worker",
    state: str = UserState.ACTIVE,
    full_access: bool = False,
    locker_ids: tuple = (WORKER_LOCKER,),
    user_id: int = 42,
) -> UserContext:
    return UserContext(
        user_id=user_id,
        role=role,
        state=state,
        full_access=full_access,
        locker_ids=locker_ids,
    )


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


class TestCheckHandler(unittest.TestCase):
    # 1. Нет X-Request-Path → 403
    def test_missing_request_path_returns_403(self):
        async def run():
            handler = _make_handler(headers={"X-Request-Method": "GET"}, cache=_make_cache())
            await handler.get()
            self.assertEqual(handler._status_code, 403)
            self.assertEqual(_problem_body(handler)["detail"], "X-Request-Path header is required")
        asyncio.run(run())

    # 2. Публичный путь — пропускаем без токена
    def test_public_path_returns_200_without_token(self):
        async def run():
            # /api/v1/auth/ — публичный согласно rules.py
            handler = _make_handler(
                headers={"X-Request-Method": "POST", "X-Request-Path": "/api/v1/auth/login"},
                cache=_make_cache(),
            )
            await handler.get()
            self.assertEqual(handler._status_code, 200)
            self.assertTrue(handler._finished)
        asyncio.run(run())

    # 3. Защищённый путь без токена → 401
    def test_protected_path_missing_token_returns_401(self):
        async def run():
            handler = _make_handler(
                headers={"X-Request-Method": "GET", "X-Request-Path": "/api/v1/users"},
                cache=_make_cache(),
            )
            await handler.get()
            self.assertEqual(handler._status_code, 401)
            self.assertEqual(_problem_body(handler)["detail"], "Missing authentication token")
        asyncio.run(run())

    # 4. _validate_token → None → 401
    def test_invalid_token_returns_401(self):
        async def run():
            handler = _make_handler(
                headers={
                    "X-Request-Method": "GET",
                    "X-Request-Path": "/api/v1/users",
                    "X-Access-Token": "bad-token",
                },
                cache=_make_cache(),
            )
            with patch.object(CheckHandler, "_validate_token", AsyncMock(return_value=None)):
                await handler.get()
            self.assertEqual(handler._status_code, 401)
            self.assertEqual(_problem_body(handler)["detail"], "Invalid or expired session")
        asyncio.run(run())

    # 5. cache.get_or_load → None → 401
    def test_user_not_found_in_cache_returns_401(self):
        async def run():
            cache = _make_cache(ctx=None)
            handler = _make_handler(
                headers={
                    "X-Request-Method": "GET",
                    "X-Request-Path": "/api/v1/users",
                    "X-Access-Token": "valid-token",
                },
                cache=cache,
            )
            with patch.object(CheckHandler, "_validate_token", AsyncMock(return_value=42)):
                await handler.get()
            self.assertEqual(handler._status_code, 401)
            self.assertEqual(_problem_body(handler)["detail"], "User not found")
            cache.get_or_load.assert_awaited_once()
        asyncio.run(run())

    # 6. ctx.state != ACTIVE → cache.invalidate + 403
    def test_inactive_user_invalidates_cache_and_returns_403(self):
        async def run():
            ctx = _ctx(role="operator", state=UserState.DEACTIVATED, full_access=True, locker_ids=())
            cache = _make_cache(ctx=ctx)
            handler = _make_handler(
                headers={
                    "X-Request-Method": "GET",
                    "X-Request-Path": "/api/v1/users",
                    "X-Access-Token": "valid-token",
                },
                cache=cache,
            )
            with patch.object(CheckHandler, "_validate_token", AsyncMock(return_value=42)):
                await handler.get()
            self.assertEqual(handler._status_code, 403)
            self.assertEqual(_problem_body(handler)["detail"], "User account is not active")
            cache.invalidate.assert_awaited_once_with(42)
        asyncio.run(run())

    # 7. Неизвестная роль → ValueError из Role.from_str → 403
    def test_unknown_role_returns_403(self):
        async def run():
            ctx = _ctx(role="superadmin")  # не входит в Role
            cache = _make_cache(ctx=ctx)
            handler = _make_handler(
                headers={
                    "X-Request-Method": "GET",
                    "X-Request-Path": "/api/v1/users",
                    "X-Access-Token": "valid-token",
                },
                cache=cache,
            )
            with patch.object(CheckHandler, "_validate_token", AsyncMock(return_value=42)):
                await handler.get()
            self.assertEqual(handler._status_code, 403)
            self.assertEqual(_problem_body(handler)["detail"], "Unknown user role")
        asyncio.run(run())

    # 8. check_access → (False, reason) → 403 с reason
    def test_role_below_required_returns_403_with_reason(self):
        async def run():
            # worker пытается зайти в /api/v1/users (требуется operator)
            ctx = _ctx(role="worker")
            cache = _make_cache(ctx=ctx)
            handler = _make_handler(
                headers={
                    "X-Request-Method": "GET",
                    "X-Request-Path": "/api/v1/users",
                    "X-Access-Token": "valid-token",
                },
                cache=cache,
            )
            with patch.object(CheckHandler, "_validate_token", AsyncMock(return_value=42)):
                await handler.get()
            self.assertEqual(handler._status_code, 403)
            detail = _problem_body(handler)["detail"]
            self.assertIn("worker", detail)
            self.assertIn("operator", detail)
        asyncio.run(run())

    # 9. Worker с Допусками → 200 + X-Remote-Lockers с UUID
    def test_worker_with_access_returns_200_with_locker_headers(self):
        async def run():
            ctx = _ctx(
                role="worker",
                full_access=False,
                locker_ids=(WORKER_LOCKER, OTHER_LOCKER),
                user_id=42,
            )
            cache = _make_cache(ctx=ctx)
            # Используем locker-scoped путь к собственному шкафу
            path = f"/api/v1/locker/{WORKER_LOCKER}/commands"
            handler = _make_handler(
                headers={
                    "X-Request-Method": "POST",
                    "X-Request-Path": path,
                    "X-Access-Token": "valid-token",
                },
                cache=cache,
            )
            with patch.object(CheckHandler, "_validate_token", AsyncMock(return_value=42)):
                await handler.get()
            self.assertEqual(handler._status_code, 200)
            self.assertEqual(handler._headers.get("X-User-Id"), "42")
            self.assertEqual(handler._headers.get("X-Remote-Role"), "worker")
            # Для worker без full_access — список UUID через запятую
            self.assertEqual(
                handler._headers.get("X-Remote-Lockers"),
                f"{WORKER_LOCKER},{OTHER_LOCKER}",
            )
        asyncio.run(run())

    # 10a. Operator → 200 + X-Remote-Lockers="*"
    def test_operator_returns_200_with_wildcard_lockers(self):
        async def run():
            ctx = _ctx(role="operator", full_access=False, locker_ids=(), user_id=7)
            cache = _make_cache(ctx=ctx)
            handler = _make_handler(
                headers={
                    "X-Request-Method": "GET",
                    "X-Request-Path": "/api/v1/users",
                    "X-Access-Token": "valid-token",
                },
                cache=cache,
            )
            with patch.object(CheckHandler, "_validate_token", AsyncMock(return_value=7)):
                await handler.get()
            self.assertEqual(handler._status_code, 200)
            self.assertEqual(handler._headers.get("X-User-Id"), "7")
            self.assertEqual(handler._headers.get("X-Remote-Role"), "operator")
            self.assertEqual(handler._headers.get("X-Remote-Lockers"), "*")
        asyncio.run(run())

    # 10b. Worker с full_access=True → X-Remote-Lockers="*"
    def test_worker_with_full_access_returns_wildcard_lockers(self):
        async def run():
            ctx = _ctx(role="worker", full_access=True, locker_ids=(), user_id=99)
            cache = _make_cache(ctx=ctx)
            path = f"/api/v1/locker/{WORKER_LOCKER}/commands"
            handler = _make_handler(
                headers={
                    "X-Request-Method": "POST",
                    "X-Request-Path": path,
                    "X-Access-Token": "valid-token",
                },
                cache=cache,
            )
            with patch.object(CheckHandler, "_validate_token", AsyncMock(return_value=99)):
                await handler.get()
            self.assertEqual(handler._status_code, 200)
            self.assertEqual(handler._headers.get("X-Remote-Lockers"), "*")
        asyncio.run(run())


class TestCheckHandlerRedisDown(unittest.TestCase):
    """Redis недоступен → деградация в cache-miss → БД-fallthrough, НЕ 500."""

    @staticmethod
    def _redis_down_mock() -> MagicMock:
        redis = MagicMock()
        redis.get = AsyncMock(side_effect=RedisConnectionError("redis down"))
        redis.set = AsyncMock(side_effect=RedisConnectionError("redis down"))
        redis.delete = AsyncMock(side_effect=RedisConnectionError("redis down"))
        return redis

    def test_redis_down_allows_decision_from_db(self):
        """operator + redis-down → 200 из БД-загруженного ctx, не 500."""
        async def run():
            cache = UserContextCache(self._redis_down_mock(), ttl_seconds=30)
            handler = _make_handler(
                headers={
                    "X-Request-Method": "GET",
                    "X-Request-Path": "/api/v1/users",
                    "X-Access-Token": "valid-token",
                },
                cache=cache,
            )
            ctx = _ctx(role="operator", full_access=False, locker_ids=(), user_id=7)
            with patch.object(CheckHandler, "_validate_token", AsyncMock(return_value=7)), \
                    patch.object(CheckHandler, "_load_user_context", AsyncMock(return_value=ctx)):
                await handler.get()
            self.assertEqual(handler._status_code, 200)
            self.assertEqual(handler._headers.get("X-User-Id"), "7")
            self.assertEqual(handler._headers.get("X-Remote-Role"), "operator")
        asyncio.run(run())

    def test_redis_down_denies_when_db_says_deny(self):
        """worker на operator-only пути + redis-down → 403 (deny из БД), не 500."""
        async def run():
            cache = UserContextCache(self._redis_down_mock(), ttl_seconds=30)
            handler = _make_handler(
                headers={
                    "X-Request-Method": "GET",
                    "X-Request-Path": "/api/v1/users",
                    "X-Access-Token": "valid-token",
                },
                cache=cache,
            )
            ctx = _ctx(role="worker", user_id=42)
            with patch.object(CheckHandler, "_validate_token", AsyncMock(return_value=42)), \
                    patch.object(CheckHandler, "_load_user_context", AsyncMock(return_value=ctx)):
                await handler.get()
            self.assertEqual(handler._status_code, 403)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
