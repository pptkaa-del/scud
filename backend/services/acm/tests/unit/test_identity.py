"""Unit-тесты app.handlers.identity.IdentityHandler.

Паттерн Astra: unittest.TestCase + asyncio.run; handler без Tornado __init__
(см. tests/unit/test_check_handler.py).
"""
from __future__ import annotations

import asyncio
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.cache import UserContext
from app.handlers.identity import IdentityHandler
from shared.enums import UserState
from shared.http import UpstreamUnavailableError


class _FakeApp:
    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.ui_methods: dict = {}
        self.ui_modules: dict = {}


class _FakeHeaders(dict):
    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _FakeRequest:
    def __init__(self, headers: dict | None = None) -> None:
        self.headers = _FakeHeaders(headers or {})
        self.method = "GET"
        self.uri = "/acm/identity"
        self.body = b""
        self.remote_ip = "127.0.0.1"
        self.cookies: dict = {}


def _make_handler(headers: dict | None = None, cache: Any = None) -> IdentityHandler:
    settings = {"cache": cache, "auth_api_url": "http://auth_api:8003"}
    h = IdentityHandler.__new__(IdentityHandler)
    h.application = _FakeApp(settings)  # type: ignore[assignment]
    h.request = _FakeRequest(headers=headers)  # type: ignore[assignment]
    h.db = object()
    h._headers = {}
    h._write_buffer = []
    h._status_code = 200
    h._reason = "OK"
    h._finished = False

    def _fake_finish(chunk=None):
        if chunk is not None:
            h._write_buffer.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        h._finished = True

    h.finish = _fake_finish  # type: ignore[assignment]
    h.flush = lambda include_footers=False: None  # type: ignore[assignment]
    return h


def _make_cache(ctx: UserContext | None = None) -> MagicMock:
    cache = MagicMock()
    cache.get_or_load = AsyncMock(return_value=ctx)
    cache.invalidate = AsyncMock(return_value=None)
    return cache


def _ctx(role: str = "worker", state: str = UserState.ACTIVE, user_id: int = 42) -> UserContext:
    return UserContext(user_id=user_id, role=role, state=state,
                       full_access=False, locker_ids=())


class TestIdentityHandler(unittest.TestCase):
    def test_missing_token_returns_401(self):
        async def run():
            h = _make_handler(headers={}, cache=_make_cache())
            await h.get()
            self.assertEqual(h._status_code, 401)
        asyncio.run(run())

    def test_invalid_token_returns_401(self):
        async def run():
            h = _make_handler(headers={"X-Access-Token": "bad"}, cache=_make_cache())
            with patch.object(IdentityHandler, "_validate_token", AsyncMock(return_value=None)):
                await h.get()
            self.assertEqual(h._status_code, 401)
        asyncio.run(run())

    def test_valid_returns_200_with_role(self):
        async def run():
            cache = _make_cache(ctx=_ctx(role="administrator", user_id=7))
            h = _make_handler(headers={"X-Access-Token": "good"}, cache=cache)
            with patch.object(IdentityHandler, "_validate_token", AsyncMock(return_value=7)):
                await h.get()
            self.assertEqual(h._status_code, 200)
            self.assertEqual(h._headers.get("X-Remote-Role"), "administrator")
            self.assertEqual(h._headers.get("X-User-Id"), "7")
            # §6.2: /acm/identity резолвит только роль, без проверки прав по
            # пути — check-only заголовки НЕ должны присутствовать в ответе.
            assert "X-Remote-Lockers" not in h._headers
            assert "X-Remote-Require-2FA" not in h._headers
        asyncio.run(run())

    def test_user_not_found_returns_401(self):
        async def run():
            h = _make_handler(headers={"X-Access-Token": "good"}, cache=_make_cache(ctx=None))
            with patch.object(IdentityHandler, "_validate_token", AsyncMock(return_value=42)):
                await h.get()
            self.assertEqual(h._status_code, 401)
        asyncio.run(run())

    def test_deactivated_returns_401_and_invalidates(self):
        async def run():
            cache = _make_cache(ctx=_ctx(state=UserState.DEACTIVATED, user_id=42))
            h = _make_handler(headers={"X-Access-Token": "good"}, cache=cache)
            with patch.object(IdentityHandler, "_validate_token", AsyncMock(return_value=42)):
                await h.get()
            self.assertEqual(h._status_code, 401)
            cache.invalidate.assert_awaited_once_with(42)
        asyncio.run(run())

    def test_auth_api_down_returns_503(self):
        async def run():
            h = _make_handler(headers={"X-Access-Token": "good"}, cache=_make_cache())
            exc = UpstreamUnavailableError("down", upstream="auth_api")
            with patch.object(IdentityHandler, "_validate_token", AsyncMock(side_effect=exc)):
                await h.get()
            self.assertEqual(h._status_code, 503)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
