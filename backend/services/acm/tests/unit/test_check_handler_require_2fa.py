"""ACM check handler propagates require_2fa to gateway via X-Remote-Require-2FA."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.cache import UserContext
from app.handlers.check import CheckHandler


def _make_handler(*, ctx: UserContext) -> CheckHandler:
    h = CheckHandler.__new__(CheckHandler)
    cache = MagicMock()
    cache.get_or_load = AsyncMock(return_value=ctx)
    cache.invalidate = AsyncMock(return_value=None)
    h.application = MagicMock(settings={"cache": cache, "auth_api_url": "http://auth_api:8003"})
    h.request = MagicMock()
    h.request.headers = {
        "X-Request-Method": "POST",
        "X-Request-Path": "/api/v1/locker/00000000-0000-0000-0000-000000000000/commands",
        "X-Access-Token": "valid-token",
    }
    h.set_status = MagicMock()
    h.set_header = MagicMock()
    h.finish = MagicMock()
    h.write_unauthorized = MagicMock()
    h.write_forbidden = MagicMock()
    h.write_service_unavailable = MagicMock()
    h._validate_token = AsyncMock(return_value=42)
    return h


def test_check_sets_require_2fa_true_header() -> None:
    async def _run() -> None:
        ctx = UserContext(
            user_id=42, role="worker", state="active",
            full_access=True, locker_ids=(),
            require_2fa=True,
        )
        h = _make_handler(ctx=ctx)
        await h.get()
        calls = {c.args[0]: c.args[1] for c in h.set_header.call_args_list}
        assert calls["X-Remote-Require-2FA"] == "true"

    asyncio.run(_run())


def test_check_sets_require_2fa_false_header() -> None:
    async def _run() -> None:
        ctx = UserContext(
            user_id=42, role="worker", state="active",
            full_access=True, locker_ids=(),
            require_2fa=False,
        )
        h = _make_handler(ctx=ctx)
        await h.get()
        calls = {c.args[0]: c.args[1] for c in h.set_header.call_args_list}
        assert calls["X-Remote-Require-2FA"] == "false"

    asyncio.run(_run())
