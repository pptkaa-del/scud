"""Unit-тесты LoginHandler: SMS-primary OTP delivery.

Покрывает:
- `_create_and_send_otp` пробрасывает `phone` в `NotificationClient.request_otp`.
- `queued_until_binding` больше не отвечает 401 max-binding-required
  (SMS-primary: телефон есть всегда, поэтому queued означает недоступность
  обоих каналов доставки) — теперь 503 service-unavailable.

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.handlers.login import OTP_DELIVERY_UNAVAILABLE_DETAIL, LoginHandler
from app.services.notification_client import OtpDeliveryStatus
from tests.unit.conftest import make_handler, make_rate_limit_repo

_TEST_PHONE = "+71112223344"


class TestCreateAndSendOtpPassesPhone(unittest.TestCase):

    async def _create_and_send_otp(self) -> tuple:
        notification_client = AsyncMock()
        notification_client.request_otp = AsyncMock(return_value=OtpDeliveryStatus.SENT)
        handler = make_handler(LoginHandler, {
            "notification_client": notification_client,
            "otp_ttl_seconds": 300,
        })
        with patch(
            "app.handlers.login.OtpRepository.create",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await handler._create_and_send_otp(
                phone=_TEST_PHONE, user_id=1, max_user_id=99,
            )
        return notification_client.request_otp.await_args

    def test_create_and_send_otp_passes_phone_through(self):
        """`_create_and_send_otp` вызывает `request_otp(..., phone=<исходный номер>)`."""
        await_args = asyncio.run(self._create_and_send_otp())
        self.assertEqual(await_args.kwargs.get("phone"), _TEST_PHONE)


class TestQueuedUntilBindingReturns503(unittest.TestCase):
    """SMS-primary: queued_until_binding больше не 401 invite-link, а 503."""

    async def _run_post_queued(self) -> dict:
        notification_client = AsyncMock()
        notification_client.request_otp = AsyncMock(
            return_value=OtpDeliveryStatus.QUEUED_UNTIL_BINDING,
        )
        handler = make_handler(LoginHandler, {
            "login_rate_limit": 10,
            "login_rate_window_seconds": 60,
            "notification_client": notification_client,
            "otp_ttl_seconds": 300,
        })
        handler.request.body = f'{{"phone":"{_TEST_PHONE}","password":"x"}}'.encode()

        captured: dict = {}

        def fake_service_unavailable(detail: str) -> None:
            captured["status"] = 503
            captured["detail"] = detail

        def fake_problem(**kwargs) -> None:
            captured["status"] = kwargs.get("status")
            captured["type_uri"] = kwargs.get("type_uri")

        with patch(
            "app.handlers.login.RateLimitRepository",
            return_value=make_rate_limit_repo(check_result=True),
        ), patch(
            "app.handlers.login.OtpRepository.create",
            new_callable=AsyncMock,
            return_value=None,
        ), patch.object(
            LoginHandler, "_check_credentials",
            new=AsyncMock(return_value=(1, True, "Имя", "worker", None)),
        ), patch.object(
            LoginHandler, "write_service_unavailable",
            side_effect=fake_service_unavailable,
        ), patch.object(
            LoginHandler, "write_problem",
            side_effect=fake_problem,
        ):
            await handler.post()
        return captured

    def test_queued_returns_503_not_401(self):
        captured = asyncio.run(self._run_post_queued())
        self.assertEqual(captured.get("status"), 503)

    def test_queued_detail_is_russian_user_facing(self):
        captured = asyncio.run(self._run_post_queued())
        self.assertEqual(captured.get("detail"), OTP_DELIVERY_UNAVAILABLE_DETAIL)

    def test_queued_does_not_call_write_problem(self):
        """Старая ветка max-binding-required (write_problem/401) больше не вызывается."""
        captured = asyncio.run(self._run_post_queued())
        self.assertNotIn("type_uri", captured)


if __name__ == "__main__":
    unittest.main()
