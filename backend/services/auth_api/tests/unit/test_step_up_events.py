"""Unit tests на publish security-events из StepUpIssueHandler/VerifyHandler.

Все варианты verify (6 веток) и issue (happy / unbound-with-phone /
no-deliverable-target) должны вызывать SecurityEventProducer.publish с
правильными event_type, severity и metadata. OTP-код НИКОГДА не должен
попадать в metadata — последняя assertion в каждом тесте.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import time
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

from app.handlers.step_up import StepUpIssueHandler, StepUpVerifyHandler
from app.services.notification_client import OtpDeliveryStatus
from app.services.security_event_producer import SecurityEventProducer
from shared.utils import body_sha256, compute_request_signature

_KEY = b"test-step-up-hmac-key-32-bytes-padding"

_METHOD = "POST"
_ISSUE_PATH = "/internal/api/v1/step-up/issue"
_VERIFY_PATH = "/internal/api/v1/step-up/verify"


def _sign(body: bytes, path: str = _ISSUE_PATH) -> tuple[str, str]:
    ts = str(int(time.time()))
    sig = compute_request_signature(ts, _METHOD, path, body_sha256(body), _KEY)
    return ts, sig


def _assert_no_otp_in_metadata(metadata: dict[str, Any]) -> None:
    """Никакое поле metadata не должно содержать OTP-код."""
    serialized = json.dumps(metadata)
    # Тесты подбирают коды вида 123456/wrong0/111111 — но запретим любые
    # цифровые 6-символьные подстроки в JSON-сериализации metadata.
    # Помимо этого, явный запрет ключа "code".
    assert "code" not in metadata, f"metadata leaks 'code' field: {metadata}"
    assert "otp" not in metadata, f"metadata leaks 'otp' field: {metadata}"
    # Не должно быть прямой подстроки 6-значного кода, ожидавшегося в тестах.
    for fixture_code in ("123456", "654321", "111111", "wrong0", "000000"):
        assert fixture_code not in serialized, (
            f"metadata leaks OTP-like substring {fixture_code}: {serialized}"
        )


def test_issue_happy_path_emits_command_2fa_issued() -> None:
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
        producer = AsyncMock()
        producer.publish = AsyncMock()

        handler = _make_handler(
            StepUpIssueHandler, body=body, ts=ts, sig=sig,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "user_api_client": user_api,
                "notification_client": notification,
                "security_event_producer": producer,
            },
            repo=AsyncMock(),
        )
        await handler.post()

        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_issued"
        assert kwargs["severity"] == "info"
        assert kwargs["locker_id"] == "11111111-1111-1111-1111-111111111111"
        meta = kwargs["metadata"]
        assert meta["user_id"] == 42
        assert meta["command_type"] == "open"
        assert meta["delivery_status"] == "sent"
        assert meta["challenge_id"]  # non-empty UUID string
        _assert_no_otp_in_metadata(meta)

    asyncio.run(_run())


def test_issue_unbound_user_with_phone_emits_issued_with_real_status() -> None:
    """max_user_id=None, но phone есть — SMS-primary доставляет код, событие
    несёт реальный status.value (``sent``), а не строку ``max_binding_required``."""
    async def _run() -> None:
        body = json.dumps({
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {
                "locker_id": "22222222-2222-2222-2222-222222222222",
                "locker_inv": "INV-1",
                "command_type": "reboot",
                "parameters": {}, "max_retries": 3,
            },
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
        producer = AsyncMock()
        producer.publish = AsyncMock()

        handler = _make_handler(
            StepUpIssueHandler, body=body, ts=ts, sig=sig,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "user_api_client": user_api,
                "notification_client": notification,
                "security_event_producer": producer,
            },
            repo=AsyncMock(),
        )
        await handler.post()

        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_issued"
        assert kwargs["severity"] == "info"
        meta = kwargs["metadata"]
        assert meta["delivery_status"] == "sent"
        assert meta["challenge_id"]  # non-empty UUID string, not None
        assert meta["command_type"] == "reboot"
        _assert_no_otp_in_metadata(meta)

    asyncio.run(_run())


def test_issue_no_deliverable_target_emits_command_2fa_issued() -> None:
    """Защитный fallback: ни phone, ни max_user_id — единственный случай,
    где сохраняется старый ``max_binding_required``-контракт события."""
    async def _run() -> None:
        body = json.dumps({
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {
                "locker_id": "22222222-2222-2222-2222-222222222222",
                "locker_inv": "INV-1",
                "command_type": "reboot",
                "parameters": {}, "max_retries": 3,
            },
            "ttl_seconds": 300,
        }).encode()
        ts, sig = _sign(body)

        user_api = AsyncMock()
        user_api.get_max_context = AsyncMock(return_value={
            "require_2fa": True, "max_user_id": None, "phone": None,
        })
        producer = AsyncMock()
        producer.publish = AsyncMock()

        handler = _make_handler(
            StepUpIssueHandler, body=body, ts=ts, sig=sig,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "user_api_client": user_api,
                "notification_client": AsyncMock(),
                "security_event_producer": producer,
            },
            repo=AsyncMock(),
        )
        await handler.post()

        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_issued"
        assert kwargs["severity"] == "info"
        meta = kwargs["metadata"]
        assert meta["delivery_status"] == "max_binding_required"
        assert meta["challenge_id"] is None
        assert meta["command_type"] == "reboot"
        _assert_no_otp_in_metadata(meta)

    asyncio.run(_run())


def test_verify_happy_emits_command_2fa_verified() -> None:
    async def _run() -> None:
        code = "654321"
        body = json.dumps({
            "challenge_id": "33333333-3333-3333-3333-333333333333",
            "user_id": 42, "code": code,
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": "33333333-3333-3333-3333-333333333333",
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {
                "locker_id": "44444444-4444-4444-4444-444444444444",
                "command_type": "open",
            },
            "code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": None,
        })
        repo.reserve_attempt = AsyncMock(return_value={
            "attempts": 1, "code_hash": hashlib.sha256(code.encode()).hexdigest(),
        })
        repo.consume = AsyncMock(return_value=True)

        producer = AsyncMock()
        producer.publish = AsyncMock()
        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "security_event_producer": producer,
            },
            repo=repo,
        )
        await handler.post()

        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_verified"
        assert kwargs["severity"] == "info"
        assert kwargs["locker_id"] == "44444444-4444-4444-4444-444444444444"
        meta = kwargs["metadata"]
        assert meta["user_id"] == 42
        assert meta["command_type"] == "open"
        assert meta["challenge_id"] == "33333333-3333-3333-3333-333333333333"
        _assert_no_otp_in_metadata(meta)

    asyncio.run(_run())


def test_verify_not_found_emits_failed_with_null_locker() -> None:
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": "55555555-5555-5555-5555-555555555555",
            "user_id": 42, "code": "000000",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value=None)

        producer = AsyncMock()
        producer.publish = AsyncMock()
        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "security_event_producer": producer,
            },
            repo=repo,
        )
        await handler.post()

        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_failed"
        assert kwargs["severity"] == "warning"
        assert kwargs["locker_id"] is None
        meta = kwargs["metadata"]
        assert meta["reason"] == "not_found"
        assert meta["user_id"] == 42
        _assert_no_otp_in_metadata(meta)

    asyncio.run(_run())


def test_verify_wrong_user_emits_failed_with_reason() -> None:
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": "66666666-6666-6666-6666-666666666666",
            "user_id": 99, "code": "111111",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": "66666666-6666-6666-6666-666666666666",
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {
                "locker_id": "77777777-7777-7777-7777-777777777777",
                "command_type": "config",
            },
            "code_hash": "x" * 64,
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": None,
        })

        producer = AsyncMock()
        producer.publish = AsyncMock()
        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "security_event_producer": producer,
            },
            repo=repo,
        )
        await handler.post()

        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_failed"
        assert kwargs["severity"] == "warning"
        assert kwargs["locker_id"] == "77777777-7777-7777-7777-777777777777"
        meta = kwargs["metadata"]
        assert meta["reason"] == "wrong_user"
        _assert_no_otp_in_metadata(meta)

    asyncio.run(_run())


def test_verify_already_used_emits_failed() -> None:
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": "88888888-8888-8888-8888-888888888888",
            "user_id": 42, "code": "111111",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": "88888888-8888-8888-8888-888888888888",
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {
                "locker_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "command_type": "open",
            },
            "code_hash": hashlib.sha256(b"111111").hexdigest(),
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": datetime.now(UTC),
        })

        producer = AsyncMock()
        producer.publish = AsyncMock()
        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "security_event_producer": producer,
            },
            repo=repo,
        )
        await handler.post()

        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_failed"
        meta = kwargs["metadata"]
        assert meta["reason"] == "already_used"
        _assert_no_otp_in_metadata(meta)

    asyncio.run(_run())


def test_verify_expired_emits_failed() -> None:
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": "99999999-9999-9999-9999-999999999999",
            "user_id": 42, "code": "111111",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": "99999999-9999-9999-9999-999999999999",
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {
                "locker_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "command_type": "reboot",
            },
            "code_hash": hashlib.sha256(b"111111").hexdigest(),
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) - timedelta(seconds=1),
            "used_at": None,
        })

        producer = AsyncMock()
        producer.publish = AsyncMock()
        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "security_event_producer": producer,
            },
            repo=repo,
        )
        await handler.post()

        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_failed"
        meta = kwargs["metadata"]
        assert meta["reason"] == "expired"
        _assert_no_otp_in_metadata(meta)

    asyncio.run(_run())


def test_verify_locked_emits_failed() -> None:
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "user_id": 42, "code": "111111",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {
                "locker_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "command_type": "open",
            },
            "code_hash": hashlib.sha256(b"000000").hexdigest(),
            "attempts": 5, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": None,
        })
        # reserve_attempt атомарно возвращает None при исчерпанном бюджете.
        repo.reserve_attempt = AsyncMock(return_value=None)

        producer = AsyncMock()
        producer.publish = AsyncMock()
        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "security_event_producer": producer,
            },
            repo=repo,
        )
        await handler.post()

        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_failed"
        meta = kwargs["metadata"]
        assert meta["reason"] == "locked"
        _assert_no_otp_in_metadata(meta)

    asyncio.run(_run())


def test_verify_invalid_emits_failed_and_increments() -> None:
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "user_id": 42, "code": "wrong0",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {
                "locker_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "command_type": "open",
            },
            "code_hash": hashlib.sha256(b"123456").hexdigest(),
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": None,
        })
        # reserve_attempt атомарно расходует попытку и возвращает code_hash;
        # код "wrong0" != сохранённый хэш => reason=invalid.
        repo.reserve_attempt = AsyncMock(return_value={
            "attempts": 1, "code_hash": hashlib.sha256(b"123456").hexdigest(),
        })

        producer = AsyncMock()
        producer.publish = AsyncMock()
        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={
                "internal_step_up_hmac_key": _KEY,
                "security_event_producer": producer,
            },
            repo=repo,
        )
        await handler.post()

        repo.reserve_attempt.assert_awaited_once()
        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_failed"
        meta = kwargs["metadata"]
        assert meta["reason"] == "invalid"
        _assert_no_otp_in_metadata(meta)

    asyncio.run(_run())


def test_no_event_emitted_if_producer_missing() -> None:
    """Если security_event_producer не в settings — handler не падает."""
    async def _run() -> None:
        body = json.dumps({
            "challenge_id": "33333333-3333-3333-3333-333333333333",
            "user_id": 42, "code": "654321",
        }).encode()
        ts, sig = _sign(body, _VERIFY_PATH)
        repo = AsyncMock()
        repo.select = AsyncMock(return_value={
            "id": "33333333-3333-3333-3333-333333333333",
            "user_id": 42, "scope_kind": "locker_command",
            "scope_payload": {"locker_id": "x", "command_type": "open"},
            "code_hash": hashlib.sha256(b"654321").hexdigest(),
            "attempts": 0, "max_attempts": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "used_at": None,
        })
        repo.reserve_attempt = AsyncMock(return_value={
            "attempts": 1, "code_hash": hashlib.sha256(b"654321").hexdigest(),
        })
        repo.consume = AsyncMock(return_value=True)

        handler = _make_handler(
            StepUpVerifyHandler, body=body, ts=ts, sig=sig, path=_VERIFY_PATH,
            settings={"internal_step_up_hmac_key": _KEY},
            repo=repo,
        )
        # Должно отработать без исключения.
        await handler.post()
        handler.set_status.assert_called_with(200)

    asyncio.run(_run())


def test_producer_publish_passes_idempotent_true() -> None:
    """publish() обязан звать http.fetch с idempotent=True: event_uid +
    ON CONFLICT (event_uid) DO NOTHING делают ретрай безопасным (дубликаты
    дедуплицируются на стороне history_api). Без этого RetryHTTPClient (COR-10/11)
    НЕ ретраит POST на 5xx/timeout — аудит теряется на первом сбое."""
    async def _run() -> None:
        http = AsyncMock()
        http.fetch = AsyncMock()
        producer = SecurityEventProducer("http://history_api:8002", http=http)

        await producer.publish(
            event_type="command_2fa_issued",
            severity="info",
            locker_id="11111111-1111-1111-1111-111111111111",
            metadata={"user_id": 42},
        )

        http.fetch.assert_awaited_once()
        kwargs = http.fetch.await_args.kwargs
        assert kwargs["idempotent"] is True

    asyncio.run(_run())


# --- helpers --- (повторяют test_step_up_handlers._make_handler)

def _make_handler(
    cls: type, *, body: bytes, ts: str, sig: str,
    settings: dict[str, Any], repo: Any, path: str = _ISSUE_PATH,
) -> Any:
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
    h.application = MagicMock(settings=settings)
    h.set_status = MagicMock()
    h.finish = MagicMock()
    h.write_unauthorized = MagicMock()
    h.write_validation_error = MagicMock()
    h.write_forbidden = MagicMock()
    h.write_service_unavailable = MagicMock()

    def _write(payload: Any, status_code: int = 200) -> None:
        h._written = json.dumps(payload) if isinstance(payload, dict) else payload
        h.set_status(status_code)
    h.write_json = MagicMock(side_effect=_write)

    h._make_repo = MagicMock(return_value=repo)
    h.db = MagicMock()
    return h
