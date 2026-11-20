"""Unit-тесты cleanup_loop (COR-28).

cleanup_loop должен периодически чистить НЕ только устаревшие токены/OTP/
challenge, но и таблицу login_rate_limit (которая иначе растёт бесконечно).
Проверяем, что одна итерация тела вызывает delete_stale у RateLimitRepository.

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio).
"""
from __future__ import annotations

import asyncio
import contextlib
import unittest
from unittest.mock import AsyncMock, patch

from app.main import _RATE_LIMIT_RETENTION_SECONDS, cleanup_loop


class TestCleanupLoop(unittest.TestCase):

    async def _run_one_iteration(self) -> AsyncMock:
        # asyncio.sleep при первом вызове возвращается, при втором — отменяет
        # цикл, так что тело отрабатывает ровно один раз.
        calls = {"n": 0}

        async def fake_sleep(_seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise asyncio.CancelledError

        delete_stale = AsyncMock()
        with patch("app.main.asyncio.sleep", new=fake_sleep), patch(
            "app.main.TokenRepository.delete_expired", new=AsyncMock(),
        ), patch(
            "app.main.OtpRepository.delete_expired", new=AsyncMock(),
        ), patch(
            "app.main.StepUpChallengeRepository.delete_expired", new=AsyncMock(),
        ), patch(
            "app.main.RateLimitRepository.delete_stale", new=delete_stale,
        ), contextlib.suppress(asyncio.CancelledError):
            await cleanup_loop(db=object())
        return delete_stale

    def test_cleanup_loop_purges_rate_limit(self) -> None:
        """cleanup_loop вызывает RateLimitRepository.delete_stale с retention."""
        delete_stale = asyncio.run(self._run_one_iteration())
        delete_stale.assert_awaited_once_with(_RATE_LIMIT_RETENTION_SECONDS)


if __name__ == "__main__":
    unittest.main()
