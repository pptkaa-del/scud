"""Unit-тесты graceful degradation ACM при сбое auth_api (5xx/сеть).

Покрывают:
- app.handlers.base._validate_token (унаследован CheckHandler'ом):
  HTTPError 5xx / сетевой сбой (OSError, ConnectionError) →
  UpstreamUnavailableError; HTTPError 4xx → None (не исключение).
- CheckHandler.get(): UpstreamUnavailableError из _validate_token → 503
  (write_service_unavailable), НЕ 401 — иначе падение auth_api маскировалось
  бы под «invalid session» (см. docstring app/handlers/check.py).

Тесты следуют паттерну Astra Linux: unittest.TestCase + asyncio.run()
вместо pytest-asyncio. Harness (_FakeApp/_FakeRequest/_make_handler/
_make_cache/_problem_body) переиспользуется из test_check_handler.py.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tornado.httpclient import HTTPError

from app.handlers import base as base_module
from app.handlers.check import CheckHandler
from shared.http import UpstreamUnavailableError
from tests.unit.test_check_handler import _make_cache, _make_handler, _problem_body


class TestValidateTokenUpstreamFailures(unittest.TestCase):
    """_validate_token: как auth_api 5xx/сетевой сбой транслируется в исключение."""

    def test_auth_api_503_raises_upstream_unavailable(self):
        async def run():
            handler = _make_handler(
                headers={"X-Access-Token": "tok"},
                cache=_make_cache(),
            )
            with patch.object(
                base_module._validate_client,
                "fetch",
                AsyncMock(side_effect=HTTPError(503)),
            ), self.assertRaises(UpstreamUnavailableError):
                await handler._validate_token("tok")
        asyncio.run(run())

    def test_auth_api_502_raises_upstream_unavailable(self):
        async def run():
            handler = _make_handler(
                headers={"X-Access-Token": "tok"},
                cache=_make_cache(),
            )
            with patch.object(
                base_module._validate_client,
                "fetch",
                AsyncMock(side_effect=HTTPError(502)),
            ), self.assertRaises(UpstreamUnavailableError):
                await handler._validate_token("tok")
        asyncio.run(run())

    def test_auth_api_connection_error_raises_upstream_unavailable(self):
        async def run():
            handler = _make_handler(
                headers={"X-Access-Token": "tok"},
                cache=_make_cache(),
            )
            with patch.object(
                base_module._validate_client,
                "fetch",
                AsyncMock(side_effect=ConnectionError("connection refused")),
            ), self.assertRaises(UpstreamUnavailableError):
                await handler._validate_token("tok")
        asyncio.run(run())

    def test_auth_api_os_error_raises_upstream_unavailable(self):
        async def run():
            handler = _make_handler(
                headers={"X-Access-Token": "tok"},
                cache=_make_cache(),
            )
            with patch.object(
                base_module._validate_client,
                "fetch",
                AsyncMock(side_effect=OSError("network unreachable")),
            ), self.assertRaises(UpstreamUnavailableError):
                await handler._validate_token("tok")
        asyncio.run(run())

    def test_auth_api_401_returns_none_not_raised(self):
        async def run():
            handler = _make_handler(
                headers={"X-Access-Token": "tok"},
                cache=_make_cache(),
            )
            with patch.object(
                base_module._validate_client,
                "fetch",
                AsyncMock(side_effect=HTTPError(401)),
            ):
                result = await handler._validate_token("tok")
            self.assertIsNone(result)
        asyncio.run(run())


class TestCheckHandlerUpstreamUnavailable(unittest.TestCase):
    """CheckHandler.get(): UpstreamUnavailableError из _validate_token → 503, не 401."""

    def test_upstream_unavailable_returns_503_not_401(self):
        async def run():
            handler = _make_handler(
                headers={
                    "X-Request-Method": "GET",
                    "X-Request-Path": "/api/v1/users",
                    "X-Access-Token": "valid-token",
                },
                cache=_make_cache(),
            )
            with patch.object(
                CheckHandler,
                "_validate_token",
                AsyncMock(side_effect=UpstreamUnavailableError("boom", upstream="auth_api")),
            ):
                await handler.get()
            self.assertEqual(handler._status_code, 503)
            self.assertIn("auth_api unreachable", _problem_body(handler)["detail"])
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
