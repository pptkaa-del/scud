"""Unit-тесты: OTP-код никогда не попадает в логи.

Раньше OTP уходил SMS-провайдеру; этот файл проверял, что _send_sms /
_create_and_send_otp не логирует код. SMS теперь нет — OTP идёт в Max-бот
через notification_api. Инвариант тот же: ни до, ни после вызова
NotificationClient.request_otp код не должен попасть в логи.
"""

import asyncio
import logging
import re
import unittest
from unittest.mock import AsyncMock, patch

from app.handlers.login import LoginHandler
from app.services.notification_client import OtpDeliveryStatus

_OTP_DIGITS_RE = re.compile(r"\d{6}")
_TEST_PHONE = "+71112223344"
_PHONE_DIGIT_RUN_RE = re.compile(r"71112223344")


class _FakeApp:
    """Минимальный заменитель tornado.web.Application — отдаёт settings."""

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.ui_methods: dict = {}
        self.ui_modules: dict = {}


def _make_handler(notification_client) -> LoginHandler:
    """Создать LoginHandler в обход Tornado __init__ (без request/application)."""
    handler = LoginHandler.__new__(LoginHandler)
    handler.application = _FakeApp({  # type: ignore[assignment]
        "notification_client": notification_client,
        "otp_ttl_seconds": 300,
    })
    handler.db = object()
    return handler


def _no_otp_leak(records: list[logging.LogRecord]) -> bool:
    """True если ни одна запись лога не содержит 6-значное число, телефон или 'OTP for'."""
    for record in records:
        message = record.getMessage()
        if _OTP_DIGITS_RE.search(message):
            return False
        if _PHONE_DIGIT_RUN_RE.search(message):
            return False
        if "OTP for" in message:
            return False
    return True


class TestOtpNotLogged(unittest.TestCase):

    async def _create_otp_max_sent(self) -> None:
        client = AsyncMock()
        client.request_otp = AsyncMock(return_value=OtpDeliveryStatus.SENT)
        handler = _make_handler(client)
        with patch(
            "app.handlers.login.OtpRepository.create",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await handler._create_and_send_otp(
                phone=_TEST_PHONE, user_id=1, max_user_id=99,
            )

    def test_otp_not_logged_on_max_sent(self):
        """Успешная отправка через Max не должна логировать код."""
        with self.assertLogs(level="DEBUG") as cm:
            logging.getLogger("test_login_no_otp_leak").debug("sentinel")
            asyncio.run(self._create_otp_max_sent())
        self.assertTrue(
            _no_otp_leak(cm.records),
            f"OTP-leak detected in logs: {[r.getMessage() for r in cm.records]}",
        )

    async def _create_otp_queued(self) -> None:
        client = AsyncMock()
        client.request_otp = AsyncMock(
            return_value=OtpDeliveryStatus.QUEUED_UNTIL_BINDING,
        )
        handler = _make_handler(client)
        with patch(
            "app.handlers.login.OtpRepository.create",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await handler._create_and_send_otp(
                phone=_TEST_PHONE, user_id=1, max_user_id=None,
            )

    def test_otp_not_logged_when_queued(self):
        """Ответ queued_until_binding (Max не привязан) — тоже без логов кода."""
        with self.assertLogs(level="DEBUG") as cm:
            logging.getLogger("test_login_no_otp_leak").debug("sentinel")
            asyncio.run(self._create_otp_queued())
        self.assertTrue(
            _no_otp_leak(cm.records),
            f"OTP-leak detected in logs: {[r.getMessage() for r in cm.records]}",
        )


if __name__ == "__main__":
    unittest.main()
