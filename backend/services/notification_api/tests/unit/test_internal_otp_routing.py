"""Unit-тесты SMS-primary/MAX-fallback маршрутизации InternalOtpSendHandler.

Сценарии (см. task-9-brief.md):
1. SMS настроен + address есть, sms.send_text ok -> 204, max не трогается.
2. sms бросает UpstreamUnavailableError, max_user_id есть -> 204 через max (fallback).
3. sms бросает RecipientUnreachableError, max_user_id есть -> 204 через max (fallback).
4. sms бросает UpstreamUnavailableError, max_user_id нет -> 503, max не трогается.
5. sms не настроен, max_user_id есть -> 204 через max (legacy-путь).
6. sms не настроен, max_user_id нет (транспортов нет вовсе) -> 200 queued_until_binding.

Каждый сценарий строит app через `make_app(db, transports=..., internal_otp_hmac_key=None)`
(dev-путь без подписи) и проверяет статус-код + какой именно мок-транспорт был awaited.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.services.transport import RecipientUnreachableError
from shared.enums import NotificationChannel
from shared.http import UpstreamUnavailableError

_URL = "/internal/api/v1/otp/send"


def _mock_transport(channel: NotificationChannel, *, side_effect: BaseException | None = None) -> MagicMock:
    transport = MagicMock()
    transport.channel = channel
    transport.send_text = AsyncMock(side_effect=side_effect)
    return transport


def _body(*, address: str | None = "+79001234567", max_user_id: int | None = 55) -> bytes:
    return json.dumps(
        {"user_id": 1, "code": "123456", "address": address, "max_user_id": max_user_id},
    ).encode()


def _patch_outbox():
    patcher = patch("app.handlers.internal_otp.OutboundMaxRepository")
    repo_cls = patcher.start()
    repo_inst = repo_cls.return_value
    repo_inst.insert = AsyncMock(return_value=42)
    repo_inst.mark_sent = AsyncMock()
    repo_inst.mark_failed = AsyncMock()
    repo_inst.increment_attempt = AsyncMock()
    return patcher, repo_inst


class TestOtpRoutingSmsPrimarySuccess(AsyncHTTPTestCase):
    """1. SMS настроен + address есть, sms.send_text ok -> 204, max не трогается."""

    def get_app(self):
        self.db_mock = MagicMock()
        self.sms_mock = _mock_transport(NotificationChannel.SMS)
        self.max_mock = _mock_transport(NotificationChannel.MAX)
        return make_app(
            self.db_mock,
            transports={
                NotificationChannel.SMS: self.sms_mock,
                NotificationChannel.MAX: self.max_mock,
            },
            internal_otp_hmac_key=None,
        )

    def test_sms_success_max_not_touched(self):
        patcher, repo_inst = _patch_outbox()
        try:
            response = self.fetch(
                _URL, method="POST", body=_body(),
                headers={"Content-Type": "application/json"},
            )
        finally:
            patcher.stop()

        self.assertEqual(response.code, 204)
        self.sms_mock.send_text.assert_awaited_once()
        self.max_mock.send_text.assert_not_awaited()
        repo_inst.mark_sent.assert_awaited_once_with(42)


class TestOtpRoutingSmsUpstreamFallbackToMax(AsyncHTTPTestCase):
    """2. sms бросает UpstreamUnavailableError, max_user_id есть -> 204 через max."""

    def get_app(self):
        self.db_mock = MagicMock()
        self.sms_mock = _mock_transport(
            NotificationChannel.SMS,
            side_effect=UpstreamUnavailableError("boom", upstream="sms_gateway"),
        )
        self.max_mock = _mock_transport(NotificationChannel.MAX)
        return make_app(
            self.db_mock,
            transports={
                NotificationChannel.SMS: self.sms_mock,
                NotificationChannel.MAX: self.max_mock,
            },
            internal_otp_hmac_key=None,
        )

    def test_upstream_unavailable_falls_back_to_max(self):
        patcher, repo_inst = _patch_outbox()
        try:
            response = self.fetch(
                _URL, method="POST", body=_body(max_user_id=77),
                headers={"Content-Type": "application/json"},
            )
        finally:
            patcher.stop()

        self.assertEqual(response.code, 204)
        self.sms_mock.send_text.assert_awaited_once()
        self.max_mock.send_text.assert_awaited_once()
        args = self.max_mock.send_text.await_args.args
        self.assertEqual(args[0], "77")
        repo_inst.increment_attempt.assert_awaited_once()
        repo_inst.mark_sent.assert_awaited_once_with(42)


class TestOtpRoutingSmsRecipientUnreachableFallbackToMax(AsyncHTTPTestCase):
    """3. sms бросает RecipientUnreachableError, max_user_id есть -> 204 через max."""

    def get_app(self):
        self.db_mock = MagicMock()
        self.sms_mock = _mock_transport(
            NotificationChannel.SMS,
            side_effect=RecipientUnreachableError("bad number", address="+79001234567"),
        )
        self.max_mock = _mock_transport(NotificationChannel.MAX)
        return make_app(
            self.db_mock,
            transports={
                NotificationChannel.SMS: self.sms_mock,
                NotificationChannel.MAX: self.max_mock,
            },
            internal_otp_hmac_key=None,
        )

    def test_recipient_unreachable_falls_back_to_max(self):
        patcher, repo_inst = _patch_outbox()
        try:
            response = self.fetch(
                _URL, method="POST", body=_body(max_user_id=88),
                headers={"Content-Type": "application/json"},
            )
        finally:
            patcher.stop()

        self.assertEqual(response.code, 204)
        self.sms_mock.send_text.assert_awaited_once()
        self.max_mock.send_text.assert_awaited_once()
        args = self.max_mock.send_text.await_args.args
        self.assertEqual(args[0], "88")
        # SMS-неудача не терминальна (есть fallback) -> mark_failed НЕ вызывается для SMS-попытки.
        repo_inst.mark_failed.assert_not_awaited()
        repo_inst.mark_sent.assert_awaited_once_with(42)


class TestOtpRoutingSmsUnavailableNoFallback(AsyncHTTPTestCase):
    """4. sms бросает UpstreamUnavailableError, max_user_id нет -> 503, max не трогается."""

    def get_app(self):
        self.db_mock = MagicMock()
        self.sms_mock = _mock_transport(
            NotificationChannel.SMS,
            side_effect=UpstreamUnavailableError("boom", upstream="sms_gateway"),
        )
        self.max_mock = _mock_transport(NotificationChannel.MAX)
        return make_app(
            self.db_mock,
            transports={
                NotificationChannel.SMS: self.sms_mock,
                NotificationChannel.MAX: self.max_mock,
            },
            internal_otp_hmac_key=None,
        )

    def test_no_binding_no_fallback_returns_503(self):
        patcher, repo_inst = _patch_outbox()
        try:
            response = self.fetch(
                _URL, method="POST", body=_body(max_user_id=None),
                headers={"Content-Type": "application/json"},
            )
        finally:
            patcher.stop()

        self.assertEqual(response.code, 503)
        self.sms_mock.send_text.assert_awaited_once()
        self.max_mock.send_text.assert_not_awaited()
        repo_inst.increment_attempt.assert_awaited_once()


class TestOtpRoutingLegacyMaxOnly(AsyncHTTPTestCase):
    """5. sms не настроен, max_user_id есть -> 204 через max (legacy-путь)."""

    def get_app(self):
        self.db_mock = MagicMock()
        self.max_mock = _mock_transport(NotificationChannel.MAX)
        return make_app(
            self.db_mock,
            transports={NotificationChannel.MAX: self.max_mock},
            internal_otp_hmac_key=None,
        )

    def test_max_only_legacy_path(self):
        patcher, repo_inst = _patch_outbox()
        try:
            response = self.fetch(
                _URL, method="POST", body=_body(max_user_id=99),
                headers={"Content-Type": "application/json"},
            )
        finally:
            patcher.stop()

        self.assertEqual(response.code, 204)
        self.max_mock.send_text.assert_awaited_once()
        args = self.max_mock.send_text.await_args.args
        self.assertEqual(args[0], "99")
        repo_inst.mark_sent.assert_awaited_once_with(42)


class TestOtpRoutingNoTransportQueued(AsyncHTTPTestCase):
    """6. Транспортов нет вовсе, max_user_id нет -> 200 queued_until_binding."""

    def get_app(self):
        self.db_mock = MagicMock()
        return make_app(self.db_mock, transports={}, internal_otp_hmac_key=None)

    def test_no_transport_returns_queued(self):
        patcher, repo_inst = _patch_outbox()
        try:
            response = self.fetch(
                _URL, method="POST", body=_body(max_user_id=None),
                headers={"Content-Type": "application/json"},
            )
        finally:
            patcher.stop()

        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body), {"status": "queued_until_binding"})
        repo_inst.insert.assert_awaited_once()
        repo_inst.mark_sent.assert_not_awaited()
        repo_inst.mark_failed.assert_not_awaited()
