"""Unit-тесты InternalOtpSendHandler — внутренний приём OTP от auth_api.

Эти тесты покрывают только MAX-канал (транспорт SMS не зарегистрирован) —
SMS-primary/fallback-маршрутизация покрыта отдельно в
``test_internal_otp_routing.py``.

Сценарии:
- 400 на невалидном теле (DTO __post_init__).
- 401 на bad/expired HMAC (если ключ настроен).
- 200 queued_until_binding если для пользователя нет ни одного транспорта
  (ни SMS, ни MAX — dev-режим).
- 204 на успешной отправке.
- 503 на UpstreamUnavailableError от Max API + outbox attempts incremented.
- 503 на MaxRecipientUnreachableError + outbox failed.
- Если HMAC-ключ не настроен — подпись не проверяется (dev-режим).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.services.max_bot_client import (
    MaxBotClient,
    MaxRecipientUnreachableError,
)
from shared.enums import NotificationChannel
from shared.http import UpstreamUnavailableError

_HMAC_KEY = b"unit-test-hmac-key"

# Канон подписи (SEC-20): timestamp\nMETHOD\npath\nsha256(body). Все тесты
# шлют POST на /internal/api/v1/otp/send.
_METHOD = "POST"
_PATH = "/internal/api/v1/otp/send"


def _sign(body: bytes, key: bytes, ts: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    body_hex = hashlib.sha256(body).hexdigest()
    msg = f"{ts}\n{_METHOD}\n{_PATH}\n{body_hex}".encode()
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return {
        "X-Internal-Timestamp": ts,
        "X-Internal-Signature": sig,
        "Content-Type": "application/json",
    }


class _OtpHandlerTestBase(AsyncHTTPTestCase):
    """Базовая сборка с моком БД и Max-клиента."""

    enable_hmac: bool = True
    enable_max_client: bool = True

    def get_app(self):
        self.db_mock = MagicMock()

        self.max_client_mock = MagicMock(spec=MaxBotClient)
        self.max_client_mock.send_text = AsyncMock()

        transports = (
            {NotificationChannel.MAX: self.max_client_mock} if self.enable_max_client else {}
        )
        return make_app(
            self.db_mock,
            transports=transports,
            internal_otp_hmac_key=_HMAC_KEY if self.enable_hmac else None,
        )


class TestInternalOtpSend(_OtpHandlerTestBase):

    def test_400_on_missing_fields(self):
        body = json.dumps({"user_id": 1}).encode()  # нет code
        response = self.fetch(
            "/internal/api/v1/otp/send",
            method="POST",
            body=body,
            headers=_sign(body, _HMAC_KEY),
        )
        self.assertEqual(response.code, 400)

    def test_400_on_negative_user_id(self):
        body = json.dumps({"user_id": -1, "code": "123456"}).encode()
        response = self.fetch(
            "/internal/api/v1/otp/send",
            method="POST",
            body=body,
            headers=_sign(body, _HMAC_KEY),
        )
        self.assertEqual(response.code, 400)

    def test_401_on_missing_signature(self):
        body = json.dumps({"user_id": 1, "code": "123456"}).encode()
        response = self.fetch(
            "/internal/api/v1/otp/send",
            method="POST",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 401)

    def test_401_on_bad_signature(self):
        body = json.dumps({"user_id": 1, "code": "123456"}).encode()
        headers = _sign(body, b"wrong-key")
        response = self.fetch(
            "/internal/api/v1/otp/send",
            method="POST",
            body=body,
            headers=headers,
        )
        self.assertEqual(response.code, 401)

    def test_401_on_expired_timestamp(self):
        body = json.dumps({"user_id": 1, "code": "123456"}).encode()
        # 60 секунд назад — за пределами окна 30s.
        headers = _sign(body, _HMAC_KEY, ts=str(int(time.time()) - 60))
        response = self.fetch(
            "/internal/api/v1/otp/send",
            method="POST",
            body=body,
            headers=headers,
        )
        self.assertEqual(response.code, 401)

    def test_204_on_successful_send(self):
        body = json.dumps({"user_id": 1, "code": "123456", "max_user_id": 99}).encode()
        with patch("app.handlers.internal_otp.OutboundMaxRepository") as repo_cls:
            repo_inst = repo_cls.return_value
            repo_inst.insert = AsyncMock(return_value=42)
            repo_inst.mark_sent = AsyncMock()
            response = self.fetch(
                "/internal/api/v1/otp/send",
                method="POST",
                body=body,
                headers=_sign(body, _HMAC_KEY),
            )
            self.assertEqual(response.code, 204)
            self.max_client_mock.send_text.assert_awaited_once()
            args = self.max_client_mock.send_text.await_args.args
            self.assertEqual(args[0], "99")
            self.assertIn("123456", args[1])
            repo_inst.mark_sent.assert_awaited_once_with(42)

    def test_503_on_upstream_unavailable(self):
        self.max_client_mock.send_text = AsyncMock(
            side_effect=UpstreamUnavailableError("boom", upstream="max_bot_api"),
        )
        body = json.dumps({"user_id": 1, "code": "123456", "max_user_id": 99}).encode()
        with patch("app.handlers.internal_otp.OutboundMaxRepository") as repo_cls:
            repo_inst = repo_cls.return_value
            repo_inst.insert = AsyncMock(return_value=42)
            repo_inst.increment_attempt = AsyncMock()
            response = self.fetch(
                "/internal/api/v1/otp/send",
                method="POST",
                body=body,
                headers=_sign(body, _HMAC_KEY),
            )
            self.assertEqual(response.code, 503)
            repo_inst.increment_attempt.assert_awaited_once()

    def test_503_on_recipient_unreachable_marks_failed(self):
        self.max_client_mock.send_text = AsyncMock(
            side_effect=MaxRecipientUnreachableError(
                "blocked", max_user_id=99, status=404,
            ),
        )
        body = json.dumps({"user_id": 1, "code": "123456", "max_user_id": 99}).encode()
        with patch("app.handlers.internal_otp.OutboundMaxRepository") as repo_cls:
            repo_inst = repo_cls.return_value
            repo_inst.insert = AsyncMock(return_value=42)
            repo_inst.mark_failed = AsyncMock()
            response = self.fetch(
                "/internal/api/v1/otp/send",
                method="POST",
                body=body,
                headers=_sign(body, _HMAC_KEY),
            )
            self.assertEqual(response.code, 503)
            repo_inst.mark_failed.assert_awaited_once()

    def test_200_queued_when_max_configured_but_user_unbound(self):
        """MAX настроен, но max_user_id=None (не привязан) — queued, БЕЗ send_text.

        Регрессия: раньше это падало в str(None) -> MaxBotClient int("None")
        -> необработанный ValueError -> 500. Теперь ранний guard ловит и
        max_t is None, и dto.max_user_id is None.
        """
        body = json.dumps({"user_id": 1, "code": "123456", "max_user_id": None}).encode()
        with patch("app.handlers.internal_otp.OutboundMaxRepository") as repo_cls:
            repo_inst = repo_cls.return_value
            repo_inst.insert = AsyncMock(return_value=42)
            response = self.fetch(
                "/internal/api/v1/otp/send",
                method="POST",
                body=body,
                headers=_sign(body, _HMAC_KEY),
            )
            self.assertEqual(response.code, 200)
            self.assertEqual(json.loads(response.body), {"status": "queued_until_binding"})
            repo_inst.insert.assert_awaited_once()
            self.max_client_mock.send_text.assert_not_awaited()


class TestInternalOtpSendNoHmac(_OtpHandlerTestBase):
    """HMAC-ключ не настроен → подпись не проверяется (dev/CI)."""

    enable_hmac = False
    enable_max_client = False

    def test_no_signature_required(self):
        body = json.dumps({"user_id": 1, "code": "123456", "max_user_id": None}).encode()
        with patch("app.handlers.internal_otp.OutboundMaxRepository") as repo_cls:
            repo_inst = repo_cls.return_value
            repo_inst.insert = AsyncMock(return_value=42)
            response = self.fetch(
                "/internal/api/v1/otp/send",
                method="POST",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response.code, 200)
            self.assertEqual(json.loads(response.body), {"status": "queued_until_binding"})


class TestInternalOtpSendNoMaxClient(_OtpHandlerTestBase):
    """Нет ни одного транспорта → ответ queued, без обращения к API."""

    enable_max_client = False

    def test_max_client_absent_returns_queued(self):
        body = json.dumps({"user_id": 1, "code": "123456", "max_user_id": 99}).encode()
        with patch("app.handlers.internal_otp.OutboundMaxRepository") as repo_cls:
            repo_inst = repo_cls.return_value
            repo_inst.insert = AsyncMock(return_value=42)
            response = self.fetch(
                "/internal/api/v1/otp/send",
                method="POST",
                body=body,
                headers=_sign(body, _HMAC_KEY),
            )
            self.assertEqual(response.code, 200)
            self.assertEqual(json.loads(response.body), {"status": "queued_until_binding"})

    def test_200_queued_when_max_user_id_null(self):
        body = json.dumps({"user_id": 1, "code": "123456", "max_user_id": None}).encode()
        with patch("app.handlers.internal_otp.OutboundMaxRepository") as repo_cls:
            repo_inst = repo_cls.return_value
            repo_inst.insert = AsyncMock(return_value=42)
            response = self.fetch(
                "/internal/api/v1/otp/send",
                method="POST",
                body=body,
                headers=_sign(body, _HMAC_KEY),
            )
            self.assertEqual(response.code, 200)
            payload = json.loads(response.body)
            self.assertEqual(payload, {"status": "queued_until_binding"})
            repo_inst.insert.assert_awaited_once()
            self.max_client_mock.send_text.assert_not_awaited()
