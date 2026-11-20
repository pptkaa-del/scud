"""Unit-тесты OTP lifecycle.

Покрывает:
- генерация OTP с TTL (login.py:_create_and_send_otp)
- отказ при истёкшем OTP (verify.py: get_active фильтрует expires_at > now)
- single-use: повторный verify помеченного OTP → 401
- consume() вызывается на успешном verify (атомарный single-use, SEC-12)

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from app.handlers.login import LoginHandler
from app.handlers.verify import VerifyHandler
from app.services.notification_client import OtpDeliveryStatus
from app.services.otp import hash_otp as _hash_otp
from tests.unit.conftest import make_handler, make_otp


class TestOtpLifecycle(unittest.TestCase):

    # ------------------------------------------------------------------
    # 1. login создаёт OTP с expires_at = now + ttl
    # ------------------------------------------------------------------
    async def _otp_generated_with_ttl(self) -> tuple[datetime, datetime]:
        otp_ttl = 600  # 10 минут
        notification_client = AsyncMock()
        notification_client.request_otp = AsyncMock(
            return_value=OtpDeliveryStatus.SENT,
        )
        handler = make_handler(LoginHandler, {
            "notification_client": notification_client,
            "otp_ttl_seconds": otp_ttl,
        })
        before = datetime.now(UTC)
        captured: dict = {}

        async def fake_create(*, phone: str, code_hash: str, expires_at: datetime):
            captured["phone"] = phone
            captured["code_hash"] = code_hash
            captured["expires_at"] = expires_at

        with patch(
            "app.handlers.login.OtpRepository.create",
            new=AsyncMock(side_effect=fake_create),
        ):
            await handler._create_and_send_otp(
                phone="+71112223344", user_id=1, max_user_id=99,
            )
        after = datetime.now(UTC)

        # Проверяем что expires_at попал в ожидаемое окно [before+ttl, after+ttl]
        self.assertIn("expires_at", captured)
        expected_low = before + timedelta(seconds=otp_ttl)
        expected_high = after + timedelta(seconds=otp_ttl)
        return captured["expires_at"], expected_low, expected_high  # type: ignore[return-value]

    def test_otp_generated_with_ttl(self):
        """login создаёт OTP с expires_at = now + ttl_seconds."""
        result = asyncio.run(self._otp_generated_with_ttl())
        actual, expected_low, expected_high = result
        self.assertGreaterEqual(actual, expected_low)
        self.assertLessEqual(actual, expected_high)

    # ------------------------------------------------------------------
    # 2. verify с истёкшим OTP → 401 (фильтрация в get_active)
    # ------------------------------------------------------------------
    async def _expired_otp_rejected(self) -> int:
        # get_active в реальном коде фильтрует `expires_at > now()` в SQL,
        # т.е. для истёкшего OTP вернёт None. Эмулируем это через mock:
        # repo.get_active("+...") → None.
        handler = make_handler(VerifyHandler, {
            "otp_max_attempts": 3,
            "access_token_ttl_hours": 1,
            "secure_cookies": False,
            "verify_rate_limit": 0,  # выключено для unit (db=object())
        }, db=object())
        handler.request.body = b'{"phone":"+71112223344","code":"123456"}'

        captured_status: dict = {}

        def fake_write_unauthorized(detail: str) -> None:
            captured_status["status"] = 401
            captured_status["detail"] = detail

        with patch(
            "app.handlers.verify.OtpRepository.get_active",
            new=AsyncMock(return_value=None),
        ), patch.object(VerifyHandler, "write_unauthorized", side_effect=fake_write_unauthorized):
            await handler.post()
        return captured_status.get("status", 0)

    def test_otp_expired_rejected(self):
        """Истёкший OTP (get_active вернул None) → 401."""
        status = asyncio.run(self._expired_otp_rejected())
        self.assertEqual(status, 401)

    # ------------------------------------------------------------------
    # 3. single-use: после consume() повторный verify → 401
    # ------------------------------------------------------------------
    async def _single_use_rejected(self) -> int:
        # OtpRepository.get_active фильтрует `used_at IS NULL` в SQL.
        # Использованный OTP → None из repo → 401.
        handler = make_handler(VerifyHandler, {
            "otp_max_attempts": 3,
            "access_token_ttl_hours": 1,
            "secure_cookies": False,
            "verify_rate_limit": 0,  # выключено для unit (db=object())
        }, db=object())
        handler.request.body = b'{"phone":"+71112223344","code":"123456"}'

        captured_status: dict = {}

        def fake_write_unauthorized(detail: str) -> None:
            captured_status["status"] = 401

        # Эмуляция: OTP помечен used_at -> get_active вернёт None
        with patch(
            "app.handlers.verify.OtpRepository.get_active",
            new=AsyncMock(return_value=None),
        ), patch.object(VerifyHandler, "write_unauthorized", side_effect=fake_write_unauthorized):
            await handler.post()
        return captured_status.get("status", 0)

    def test_otp_single_use(self):
        """OTP, сожжённый consume(), не возвращается get_active → 401 при повторе."""
        status = asyncio.run(self._single_use_rejected())
        self.assertEqual(status, 401)

    # ------------------------------------------------------------------
    # 4. verify успех → consume() вызван (атомарный single-use, SEC-12)
    # ------------------------------------------------------------------
    async def _consume_called_on_success(self) -> tuple[int, int]:
        code = "123456"
        otp = make_otp(otp_id=77, code_hash=_hash_otp(code), attempts=0)

        handler = make_handler(VerifyHandler, {
            "otp_max_attempts": 3,
            "access_token_ttl_hours": 1,
            "secure_cookies": False,
            "user_api_client": object(),
            "verify_rate_limit": 0,  # выключено для unit (db=object())
        }, db=object())
        handler.request.body = f'{{"phone":"+71112223344","code":"{code}"}}'.encode()

        consume_mock = AsyncMock(return_value=True)

        # _get_user_by_phone вернёт valid запись; _issue_access_token замокан
        with patch(
            "app.handlers.verify.OtpRepository.get_active",
            new=AsyncMock(return_value=otp),
        ), patch(
            "app.handlers.verify.OtpRepository.reserve_attempt",
            new=AsyncMock(return_value={"attempts": 1, "code_hash": otp.code_hash}),
        ), patch(
            "app.handlers.verify.OtpRepository.consume",
            new=consume_mock,
        ), patch.object(
            VerifyHandler, "_get_user_by_phone",
            new=AsyncMock(return_value={"user_id": 42, "name": "n", "role": "worker"}),
        ), patch.object(
            VerifyHandler, "_issue_access_token",
            new=AsyncMock(return_value="raw_token_value"),
        ), patch.object(
            VerifyHandler, "_set_access_cookie", return_value=None,
        ), patch.object(
            VerifyHandler, "write_json", return_value=None,
        ):
            await handler.post()

        return consume_mock.await_count, consume_mock.await_args.args[0] if consume_mock.await_args else -1

    def test_otp_marked_used_on_login_success(self):
        """После успешного verify вызывается consume(otp.id) ровно один раз."""
        count, otp_id_arg = asyncio.run(self._consume_called_on_success())
        self.assertEqual(count, 1)
        self.assertEqual(otp_id_arg, 77)


if __name__ == "__main__":
    unittest.main()
