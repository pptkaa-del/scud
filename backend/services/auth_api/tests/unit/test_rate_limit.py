"""Unit-тесты DB-backed rate-limit для login.py.

Покрывает:
- разрешение запроса когда RateLimitRepository.check_and_record возвращает True
- блокировка (429) когда возвращается False
- независимость per-key (две разных IP — два разных ключа)
- end-to-end: LoginHandler.post() возвращает 429 при заблокированном IP

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio).
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.handlers.login import LoginHandler
from tests.unit.conftest import (
    make_handler,
    make_rate_limit_repo,
)

RATE_LIMIT = 5
RATE_WINDOW = 60


def _base_settings() -> dict:
    return {
        "login_rate_limit": RATE_LIMIT,
        "login_rate_window_seconds": RATE_WINDOW,
        "user_api_client": object(),
    }


class TestRateLimitHandlerBlocksOn429(unittest.TestCase):
    """Когда check_and_record возвращает False, LoginHandler.post() → 429."""

    async def _run_post_with_repo(self, repo: MagicMock, ip: str = "10.1.2.3") -> dict:
        handler = make_handler(LoginHandler, _base_settings())
        handler.request.remote_ip = ip
        handler.request.body = b'{"phone":"+71112223344","password":"x"}'

        captured: dict = {}

        def fake_too_many(detail: str) -> None:
            captured["status"] = 429
            captured["detail"] = detail

        def fake_unauthorized(detail: str) -> None:
            captured["status"] = 401

        def fake_validation(detail: str) -> None:
            captured["status"] = 400

        with patch(
            "app.handlers.login.RateLimitRepository",
            return_value=repo,
        ), patch.object(
            LoginHandler, "write_too_many_requests",
            side_effect=fake_too_many,
        ), patch.object(
            LoginHandler, "write_unauthorized",
            side_effect=fake_unauthorized,
        ), patch.object(
            LoginHandler, "write_validation_error",
            side_effect=fake_validation,
        ):
            await handler.post()
        return captured

    def test_login_returns_429_when_rate_limit_exhausted(self):
        """check_and_record=False → 429 c сообщением 'Too many login attempts'."""
        repo = make_rate_limit_repo(check_result=False)
        captured = asyncio.run(self._run_post_with_repo(repo))
        self.assertEqual(captured.get("status"), 429)
        self.assertIn("Too many login attempts", captured.get("detail", ""))
        repo.check_and_record.assert_awaited_once_with(
            "10.1.2.3", RATE_LIMIT, RATE_WINDOW,
        )

    def test_rate_limit_blocks_after_threshold(self):
        """Первые 5 проходят (mock=True), 6-я (mock=False) → 429."""
        # Имитируем 5 успешных + 1 заблокированную попытку через side_effect
        repo = MagicMock()
        repo.check_and_record = AsyncMock(
            side_effect=[True, True, True, True, True, False]
        )

        async def run_six() -> list[int]:
            statuses: list[int] = []
            for _ in range(6):
                handler = make_handler(LoginHandler, _base_settings())
                handler.request.remote_ip = "10.0.0.1"
                handler.request.body = b'{"phone":"","password":""}'

                slot: dict = {}

                # B023 noqa: slot используется синхронно в той же итерации.
                def too_many(detail: str) -> None:
                    slot["status"] = 429  # noqa: B023

                def validation(detail: str) -> None:
                    slot["status"] = 400  # noqa: B023

                with patch(
                    "app.handlers.login.RateLimitRepository",
                    return_value=repo,
                ), patch.object(
                    LoginHandler, "write_too_many_requests",
                    side_effect=too_many,
                ), patch.object(
                    LoginHandler, "write_validation_error",
                    side_effect=validation,
                ):
                    await handler.post()
                statuses.append(slot.get("status", 0))
            return statuses

        statuses = asyncio.run(run_six())
        # Первые 5 — не 429 (валидация падает на пустых полях с 400),
        # 6-я — именно 429.
        self.assertEqual(statuses[5], 429)
        for i, s in enumerate(statuses[:5]):
            self.assertNotEqual(s, 429, f"attempt #{i+1} should not be 429")
        self.assertEqual(repo.check_and_record.await_count, 6)


class TestRateLimitWindowReset(unittest.TestCase):
    """После «истечения окна» БД-репо возвращает True снова — handler пропускает."""

    def test_rate_limit_window_reset(self):
        """Эмулируем: сначала True×5, затем False, затем True (после окна) — последняя попытка проходит."""
        repo = MagicMock()
        repo.check_and_record = AsyncMock(
            side_effect=[True, True, True, True, True, False, True]
        )

        async def run_seven() -> list[int]:
            statuses: list[int] = []
            for _ in range(7):
                handler = make_handler(LoginHandler, _base_settings())
                handler.request.remote_ip = "10.0.0.2"
                handler.request.body = b'{"phone":"","password":""}'

                slot: dict = {}

                # B023 noqa: slot используется синхронно в той же итерации.
                def too_many(detail: str) -> None:
                    slot["status"] = 429  # noqa: B023

                def validation(detail: str) -> None:
                    slot["status"] = 400  # noqa: B023

                with patch(
                    "app.handlers.login.RateLimitRepository",
                    return_value=repo,
                ), patch.object(
                    LoginHandler, "write_too_many_requests",
                    side_effect=too_many,
                ), patch.object(
                    LoginHandler, "write_validation_error",
                    side_effect=validation,
                ):
                    await handler.post()
                statuses.append(slot.get("status", 0))
            return statuses

        statuses = asyncio.run(run_seven())
        self.assertEqual(statuses[5], 429, "6-я попытка — заблокирована")
        self.assertNotEqual(
            statuses[6], 429,
            "после сброса окна репо снова возвращает True → не 429",
        )


class TestRateLimitIndependentKeys(unittest.TestCase):
    """check_and_record получает разные ключи для разных IP."""

    def test_rate_limit_independent_keys(self):
        """Два разных IP → check_and_record вызывается с разными key-аргументами."""
        repo = MagicMock()
        # IP_A: True (allow), IP_B: True (allow) — handler идёт дальше
        repo.check_and_record = AsyncMock(return_value=True)

        async def run_two() -> None:
            for ip in ("10.0.0.10", "10.0.0.20"):
                handler = make_handler(LoginHandler, _base_settings())
                handler.request.remote_ip = ip
                handler.request.body = b'{"phone":"","password":""}'

                with patch(
                    "app.handlers.login.RateLimitRepository",
                    return_value=repo,
                ), patch.object(
                    LoginHandler, "write_too_many_requests",
                ), patch.object(
                    LoginHandler, "write_validation_error",
                ):
                    await handler.post()

        asyncio.run(run_two())

        self.assertEqual(repo.check_and_record.await_count, 2)
        call_keys = [c.args[0] for c in repo.check_and_record.await_args_list]
        self.assertEqual(call_keys, ["10.0.0.10", "10.0.0.20"])


if __name__ == "__main__":
    unittest.main()
