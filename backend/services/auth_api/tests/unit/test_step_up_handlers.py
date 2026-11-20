"""Unit tests для StepUpIssueHandler и StepUpVerifyHandler.

Pytest-asyncio в Astra отсутствует — все async-проверки в asyncio.run.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import time
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

from app.handlers.login import OTP_DELIVERY_UNAVAILABLE_DETAIL
from app.handlers.step_up import (
    StepUpIssueHandler,
    StepUpVerifyHandler,
)
from app.services.notification_client import OtpDeliveryStatus
from shared.http import UpstreamUnavailableError
from shared.utils import body_sha256, compute_request_signature

_KEY = b"test-step-up-hmac-key-32-bytes-padding"

# Подписываемые (method, path) step-up endpoint'ов. StepUpIssueHandler и
# StepUpVerifyHandler делят _verify_signature, но живут на разных путях.
_METHOD = "POST"
_ISSUE_PATH = "/internal/api/v1/step-up/issue"
_VERIFY_PATH = "/internal/api/v1/step-up/verify"

# Валидный UUID-челлендж для verify-тестов (VAL-02: не-UUID challenge_id
# короткозамыкается в 401 ДО repo.select, поэтому тесты бизнес-веток ниже
# обязаны использовать настоящий UUID).
_VERIFY_CID = "44444444-4444-4444-4444-444444444444"


def _sign(body: bytes, path: str = _ISSUE_PATH) -> tuple[str, str]:
    ts = str(int(time.time()))
    sig = compute_request_signature(ts, _METHOD, path, body_sha256(body), _KEY)
    return ts, sig


def test_issue_happy_path_returns_201_with_challenge_id() -> None:
    async def _run() -> None:
        body = json.dumps({
            "user_id": 42,
            "scope_kind": "locker_command",
            "scope_payload": {
                "locker_id": "11111111-1111-1111-1111-111111111111",
                "locker_inv": "INV-0042",
                "command_type": "open",
                "parameters": {},
                "max_retries": 3,
            },
            "ttl_seconds": 300,
        }).encode()
        ts, sig = _sign(body)

        user_api = AsyncMock()
        user_api.get_max_context = AsyncMock(return_value={
            "require_2fa": True, "max_user_id": 555000,
            "phone": "+79991234567",
        })
        notification = AsyncMock()
        notification.request_otp = AsyncMock(return_value=OtpDeliveryStatus.SENT)
        repo = AsyncMock()
        captured_id = {}

        async def _insert(**kwargs):
            captured_id["id"] = kwargs["challenge_id"]
        repo.insert = AsyncMock(side_effect=_insert)

        handler = _make_handler(
            StepUpIssueHandler, body=body, ts=ts, sig=sig,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "user_api_client": user_api,
                "notification_client": notification,
            },
            repo=repo,
        )
        await handler.post()

        handler.set_status.assert_called_with(201)
        payload = json.loads(handler._written)
        assert payload["challenge_id"] == captured_id["id"]
        assert payload["delivery_status"] == OtpDeliveryStatus.SENT.value
        assert "expires_at" in payload
        notification.request_otp.assert_awaited_once()
        kwargs = notification.request_otp.await_args.kwargs
        assert kwargs["user_id"] == 42
        assert kwargs["max_user_id"] == 555000
        assert kwargs["purpose"] == "otp_command"
        assert kwargs["phone"] == "+79991234567"
        assert len(kwargs["code"]) == 6

    asyncio.run(_run())


def test_issue_replay_window_rejects_old_timestamp() -> None:
    async def _run() -> None:
        body = b'{"user_id":42,"scope_kind":"locker_command","scope_payload":{},"ttl_seconds":300}'
        old_ts = str(int(time.time()) - 120)
        sig = compute_request_signature(
            old_ts, _METHOD, _ISSUE_PATH, body_sha256(body), _KEY,
        )

        handler = _make_handler(
            StepUpIssueHandler, body=body, ts=old_ts, sig=sig,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=AsyncMock(),
        )
        await handler.post()
        handler.write_unauthorized.assert_called_once()

    asyncio.run(_run())


def test_issue_bad_signature_returns_401() -> None:
    async def _run() -> None:
        body = b'{"user_id":42,"scope_kind":"locker_command","scope_payload":{},"ttl_seconds":300}'
        ts = str(int(time.time()))
        handler = _make_handler(
            StepUpIssueHandler, body=body, ts=ts, sig="0" * 64,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=AsyncMock(),
        )
        await handler.post()
        handler.write_unauthorized.assert_called_once()

    asyncio.run(_run())


def test_issue_signature_for_other_path_returns_401() -> None:
    """SEC-20: подпись, снятая для verify-пути, не принимается issue-handler'ом."""
    async def _run() -> None:
        body = b'{"user_id":42,"scope_kind":"locker_command","scope_payload":{},"ttl_seconds":300}'
        # Подписали для _VERIFY_PATH, а handler сидит на _ISSUE_PATH.
        ts, sig = _sign(body, _VERIFY_PATH)
        handler = _make_handler(
            StepUpIssueHandler, body=body, ts=ts, sig=sig,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=AsyncMock(),
            path=_ISSUE_PATH,
        )
        await handler.post()
        handler.write_unauthorized.assert_called_once()

    asyncio.run(_run())


def test_issue_require_2fa_false_returns_400() -> None:
    async def _run() -> None:
        body = json.dumps({
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {"locker_id": "x", "locker_inv": "i",
                              "command_type": "open", "parameters": {}, "max_retries": 3},
            "ttl_seconds": 300,
        }).encode()
        ts, sig = _sign(body)

        user_api = AsyncMock()
        user_api.get_max_context = AsyncMock(return_value={
            "require_2fa": False, "max_user_id": 1,
        })
        handler = _make_handler(
            StepUpIssueHandler, body=body, ts=ts, sig=sig,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "user_api_client": user_api,
                "notification_client": AsyncMock(),
            },
            repo=AsyncMock(),
        )
        await handler.post()
        handler.write_validation_error.assert_called_once()

    asyncio.run(_run())


def test_issue_unbound_user_with_phone_sends_sms_creates_challenge() -> None:
    """Пользователь включил 2FA, но не привязал Max — users.phone NOT NULL,
    поэтому SMS-primary доставка проходит и challenge создаётся как обычно
    (никакого max_binding_required, это больше не хард-гейт)."""
    async def _run() -> None:
        body = json.dumps({
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {"locker_id": "x", "locker_inv": "i",
                              "command_type": "open", "parameters": {}, "max_retries": 3},
            "ttl_seconds": 300,
        }).encode()
        ts, sig = _sign(body)

        user_api = AsyncMock()
        user_api.get_max_context = AsyncMock(return_value={
            "require_2fa": True, "max_user_id": None,
            "phone": "+79991234567",
        })
        notification = AsyncMock()
        notification.request_otp = AsyncMock(return_value=OtpDeliveryStatus.SENT)
        repo = AsyncMock()
        captured_id = {}

        async def _insert(**kwargs):
            captured_id["id"] = kwargs["challenge_id"]
        repo.insert = AsyncMock(side_effect=_insert)

        handler = _make_handler(
            StepUpIssueHandler, body=body, ts=ts, sig=sig,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "user_api_client": user_api,
                "notification_client": notification,
            },
            repo=repo,
        )
        await handler.post()

        repo.insert.assert_awaited_once()
        notification.request_otp.assert_awaited_once()
        kwargs = notification.request_otp.await_args.kwargs
        assert kwargs["max_user_id"] is None
        assert kwargs["phone"] == "+79991234567"

        handler.set_status.assert_called_with(201)
        payload = json.loads(handler._written)
        assert payload["challenge_id"] == captured_id["id"]
        assert payload["delivery_status"] == OtpDeliveryStatus.SENT.value

    asyncio.run(_run())


def test_issue_no_deliverable_target_returns_max_binding_required() -> None:
    """Защитный fallback: и phone, и max_user_id отсутствуют — доставить
    некуда. Практически недостижимо (users.phone NOT NULL), но контракт
    сохраняется на случай действительно недоступного пользователя: 201 с
    null challenge_id, никакого challenge в БД, никакого вызова notification."""
    async def _run() -> None:
        body = json.dumps({
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {"locker_id": "x", "locker_inv": "i",
                              "command_type": "open", "parameters": {}, "max_retries": 3},
            "ttl_seconds": 300,
        }).encode()
        ts, sig = _sign(body)

        user_api = AsyncMock()
        user_api.get_max_context = AsyncMock(return_value={
            "require_2fa": True, "max_user_id": None, "phone": None,
        })
        notification = AsyncMock()
        repo = AsyncMock()
        handler = _make_handler(
            StepUpIssueHandler, body=body, ts=ts, sig=sig,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "user_api_client": user_api,
                "notification_client": notification,
            },
            repo=repo,
        )
        await handler.post()
        handler.set_status.assert_called_with(201)
        payload = json.loads(handler._written)
        assert payload["challenge_id"] is None
        assert payload["delivery_status"] == "max_binding_required"
        repo.insert.assert_not_awaited()
        notification.request_otp.assert_not_awaited()

    asyncio.run(_run())


def _issue_body() -> bytes:
    return json.dumps({
        "user_id": 42, "scope_kind": "locker_command",
        "scope_payload": {"locker_id": "x", "locker_inv": "i",
                          "command_type": "open", "parameters": {}, "max_retries": 3},
        "ttl_seconds": 300,
    }).encode()


def _issue_with_notification(
    notification: AsyncMock,
) -> tuple[StepUpIssueHandler, AsyncMock, dict[str, str]]:
    body = _issue_body()
    ts, sig = _sign(body)
    user_api = AsyncMock()
    user_api.get_max_context = AsyncMock(return_value={
        "require_2fa": True, "max_user_id": None, "phone": "+79991234567",
    })
    repo = AsyncMock()
    captured: dict[str, str] = {}

    async def _insert(**kwargs):
        captured["id"] = kwargs["challenge_id"]
    repo.insert = AsyncMock(side_effect=_insert)
    handler = _make_handler(
        StepUpIssueHandler, body=body, ts=ts, sig=sig,
        settings={
            "internal_step_up_hmac_key": _KEY,
            "user_api_client": user_api,
            "notification_client": notification,
        },
        repo=repo,
    )
    return handler, repo, captured


def test_issue_queued_until_binding_returns_201_and_keeps_challenge() -> None:
    """SMS не доставлен и Max не привязан: код ждёт привязки в outbox, issue
    отвечает 201 с challenge_id и delivery_status=queued_until_binding."""
    async def _run() -> None:
        notification = AsyncMock()
        notification.request_otp = AsyncMock(
            return_value=OtpDeliveryStatus.QUEUED_UNTIL_BINDING,
        )
        handler, repo, captured = _issue_with_notification(notification)
        await handler.post()

        handler.set_status.assert_called_with(201)
        handler.write_service_unavailable.assert_not_called()
        repo.consume.assert_not_awaited()
        payload = json.loads(handler._written)
        assert payload["challenge_id"] == captured["id"]
        assert payload["delivery_status"] == "queued_until_binding"

    asyncio.run(_run())


def test_issue_notification_unavailable_returns_503() -> None:
    async def _run() -> None:
        notification = AsyncMock()
        notification.request_otp = AsyncMock(
            side_effect=UpstreamUnavailableError("down", upstream="notification_api"),
        )
        handler, _repo, _captured = _issue_with_notification(notification)
        await handler.post()

        handler.write_service_unavailable.assert_called_once_with(
            OTP_DELIVERY_UNAVAILABLE_DETAIL,
        )
        handler.write_json.assert_not_called()

    asyncio.run(_run())


def test_verify_happy_path_returns_scope_payload() -> None:
    async def _run() -> None:
        challenge_id = "22222222-2222-2222-2222-222222222222"
        code = "123456"
        body = json.dumps({
            "challenge_id": challenge_id,
            "user_id": 42,
            "code": code,
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)

        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": challenge_id, "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {"locker_id": "x", "command_type": "open"},
            "code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": None,
        })
        repo.reserve_attempt = AsyncMock(return_value={
            "attempts": 1,
            "code_hash": hashlib.sha256(code.encode()).hexdigest(),
        })
        repo.consume = AsyncMock(return_value=True)

        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=repo,
        )
        await handler.post()
        repo.consume.assert_awaited_once_with(challenge_id)
        handler.set_status.assert_called_with(200)
        payload = json.loads(handler._written)
        assert payload["scope_kind"] == "locker_command"
        assert payload["scope_payload"]["command_type"] == "open"

    asyncio.run(_run())


def test_verify_wrong_user_returns_403() -> None:
    async def _run() -> None:
        challenge_id = "22222222-2222-2222-2222-222222222222"
        body = json.dumps({
            "challenge_id": challenge_id, "user_id": 99, "code": "123456",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": challenge_id, "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {}, "code_hash": "x" * 64,
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": None,
        })

        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=repo,
        )
        await handler.post()
        handler.write_forbidden.assert_called_once()

    asyncio.run(_run())


def test_verify_used_returns_401() -> None:
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": _VERIFY_CID, "user_id": 42, "code": "123456",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": _VERIFY_CID, "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {}, "code_hash": hashlib.sha256(b"123456").hexdigest(),
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": datetime.now(UTC),
        })

        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=repo,
        )
        await handler.post()
        handler.write_unauthorized.assert_called_once()

    asyncio.run(_run())


def test_verify_expired_returns_401() -> None:
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": _VERIFY_CID, "user_id": 42, "code": "123456",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": _VERIFY_CID, "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {}, "code_hash": hashlib.sha256(b"123456").hexdigest(),
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) - timedelta(seconds=1),
            "used_at": None,
        })

        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=repo,
        )
        await handler.post()
        handler.write_unauthorized.assert_called_once()

    asyncio.run(_run())


def test_verify_locked_returns_401() -> None:
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": _VERIFY_CID, "user_id": 42, "code": "123456",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": _VERIFY_CID, "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {}, "code_hash": hashlib.sha256(b"000000").hexdigest(),
            "attempts": 5, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": None,
        })
        # reserve_attempt атомарно возвращает None при исчерпанном бюджете.
        repo.reserve_attempt = AsyncMock(return_value=None)

        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=repo,
        )
        await handler.post()
        handler.write_unauthorized.assert_called_once()

    asyncio.run(_run())


def test_verify_invalid_code_increments_attempts_and_returns_401() -> None:
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": _VERIFY_CID, "user_id": 42, "code": "wrong0",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": _VERIFY_CID, "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {}, "code_hash": hashlib.sha256(b"123456").hexdigest(),
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": None,
        })
        # reserve_attempt атомарно резервирует попытку и возвращает code_hash;
        # код "wrong0" != сохранённый хэш => 401.
        repo.reserve_attempt = AsyncMock(return_value={
            "attempts": 1,
            "code_hash": hashlib.sha256(b"123456").hexdigest(),
        })

        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=repo,
        )
        await handler.post()
        repo.reserve_attempt.assert_awaited_once_with(_VERIFY_CID)
        handler.write_unauthorized.assert_called_once()

    asyncio.run(_run())


def test_verify_malformed_challenge_id_returns_401_not_500() -> None:
    """VAL-02: не-UUID challenge_id → 401 (как not-found), не 400/500/503.

    Малформенный challenge_id неотличим от неизвестного: тот же
    write_unauthorized, repo.select даже не вызывается (DB-ошибки нет).
    """
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": "not-a-uuid", "user_id": 42, "code": "123456",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock()

        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=repo,
        )
        await handler.post()

        handler.write_unauthorized.assert_called_once()
        # Малформенный id не должен бить в БД (иначе был бы 500→503).
        repo.select.assert_not_awaited()
        # И точно не 400.
        handler.write_validation_error.assert_not_called()

    asyncio.run(_run())


def test_verify_uses_constant_time_hash_compare() -> None:
    """Step-up сравнивает хэш OTP через hmac.compare_digest (SEC-19).

    Шпионим за hmac.compare_digest в модуле step_up: при верном коде он
    должен быть вызван с (hash(code), reserved_code_hash). Если кто-то
    вернёт `==`, шпион не будет вызван и тест упадёт.
    """
    async def _run() -> None:
        challenge_id = "33333333-3333-3333-3333-333333333333"
        code = "246810"
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        body = json.dumps({
            "challenge_id": challenge_id, "user_id": 42, "code": code,
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)

        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": challenge_id, "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {"locker_id": "x", "command_type": "open"},
            "code_hash": code_hash, "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": None,
        })
        repo.reserve_attempt = AsyncMock(return_value={
            "attempts": 1, "code_hash": code_hash,
        })
        repo.consume = AsyncMock(return_value=True)

        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=repo,
        )

        real_compare = hmac.compare_digest
        spy = MagicMock(side_effect=real_compare)
        with patch("app.handlers.step_up.hmac.compare_digest", spy):
            await handler.post()

        # Сравнение хэша кода идёт через hmac.compare_digest, а не ==.
        # (HMAC-проверка подписи живёт в shared.utils и сюда не считается.)
        spy.assert_any_call(code_hash, code_hash)
        handler.set_status.assert_called_with(200)

    asyncio.run(_run())


# --- helpers ---

def _make_handler(cls, *, body, ts, sig, settings, repo, path=_ISSUE_PATH):
    """Минимальный harness вокруг ProblemDetailsHandler без Tornado-инициализации.

    Мы заменяем self.request / self.settings / self.set_status / self.write /
    self.write_unauthorized / self.write_validation_error / self.write_forbidden
    мок-объектами, чтобы протестировать pure-business-логику handler'а.
    """
    h = cls.__new__(cls)
    h._written = ""

    class _Req:
        headers: ClassVar[dict[str, str]] = {
            "X-Internal-Timestamp": ts,
            "X-Internal-Signature": sig,
        }
        remote_ip = "127.0.0.1"
        method = _METHOD
    _Req.body = body
    _Req.path = path
    h.request = _Req()
    # Tornado `self.settings` — это property → application.settings;
    # подменяем application, чтобы property вернул наш dict.
    h.application = MagicMock(settings=settings)
    h.set_status = MagicMock()
    h.finish = MagicMock()
    h.write_unauthorized = MagicMock()
    h.write_validation_error = MagicMock()
    h.write_forbidden = MagicMock()
    h.write_service_unavailable = MagicMock()

    def _write(payload, status_code=200):
        h._written = json.dumps(payload) if isinstance(payload, dict) else payload
        h.set_status(status_code)
    h.write_json = MagicMock(side_effect=_write)

    # Подменяем создание репозитория на наш мок.
    h._make_repo = MagicMock(return_value=repo)
    h.db = MagicMock()
    return h
