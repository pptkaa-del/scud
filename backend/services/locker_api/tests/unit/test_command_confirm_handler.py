"""CommandConfirmHandler — verify, locker_id mismatch, scope mismatch, OK."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import app.handlers.command_confirm as mod
from app.handlers.command_confirm import CommandConfirmHandler
from app.services.auth_step_up_client import (
    StepUpVerifyError,
    StepUpVerifyResult,
)


def _make_handler(
    client,
    *,
    role: str = "worker",
    current_user_id: int = 1,
    body: dict | None = None,
    producer=None,
):
    h = CommandConfirmHandler.__new__(CommandConfirmHandler)
    settings = {"auth_step_up_client": client}
    if producer is not None:
        settings["security_event_producer"] = producer
    # Tornado settings — read-only @property; mock через application.settings.
    h.application = MagicMock(settings=settings)
    h.request = MagicMock()
    h.request.headers = {"X-Remote-Role": role}
    h._current_user_id = current_user_id
    h._written = None

    def _w(payload, status_code=200):
        h._written = payload
        h.set_status(status_code)
    h.write_json = MagicMock(side_effect=_w)
    h.set_status = MagicMock()
    h.write_unauthorized = MagicMock()
    h.write_forbidden = MagicMock()
    h.write_validation_error = MagicMock()
    h.write_not_found = MagicMock()
    h.write_service_unavailable = MagicMock()
    h.get_json_body = MagicMock(return_value=body or {
        "challenge_id": "cid", "code": "123456",
    })
    h._enqueue_command = AsyncMock()
    h.db = MagicMock()
    return h


def test_verify_ok_enqueues_command() -> None:
    async def _run() -> None:
        locker_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        client = AsyncMock()
        client.verify = AsyncMock(return_value=StepUpVerifyResult(
            scope_kind="locker_command",
            scope_payload={
                "locker_id": locker_uuid,
                "locker_inv": "INV-01",
                "command_type": "open",
                "parameters": {},
                "max_retries": 3,
            },
        ))
        h = _make_handler(client)
        locker = MagicMock()
        locker.id = locker_uuid
        locker.inventory_number = "INV-01"
        original_repo = mod.LockerRepository
        try:
            mod.LockerRepository = MagicMock(
                return_value=MagicMock(get_by_id=AsyncMock(return_value=locker)),
            )
            await h.post(locker_uuid)
        finally:
            mod.LockerRepository = original_repo
        h._enqueue_command.assert_awaited_once()

    asyncio.run(_run())


def test_locker_id_mismatch_returns_403() -> None:
    async def _run() -> None:
        client = AsyncMock()
        client.verify = AsyncMock(return_value=StepUpVerifyResult(
            scope_kind="locker_command",
            scope_payload={
                "locker_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "command_type": "open",
                "parameters": {},
                "max_retries": 3,
            },
        ))
        h = _make_handler(client)
        await h.post("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        h.write_forbidden.assert_called_once()
        h._enqueue_command.assert_not_awaited()

    asyncio.run(_run())


def test_invalid_code_returns_401() -> None:
    async def _run() -> None:
        client = AsyncMock()
        client.verify = AsyncMock(return_value=StepUpVerifyError(
            status_code=401, detail="Invalid code",
        ))
        h = _make_handler(client)
        await h.post("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        h.write_unauthorized.assert_called_once()
        h._enqueue_command.assert_not_awaited()

    asyncio.run(_run())


def test_wrong_user_returns_403() -> None:
    async def _run() -> None:
        client = AsyncMock()
        client.verify = AsyncMock(return_value=StepUpVerifyError(
            status_code=403, detail="Wrong user",
        ))
        h = _make_handler(client)
        await h.post("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        h.write_forbidden.assert_called_once()
        h._enqueue_command.assert_not_awaited()

    asyncio.run(_run())


def test_upstream_unavailable_returns_503() -> None:
    async def _run() -> None:
        from shared.http import UpstreamUnavailableError
        client = AsyncMock()
        client.verify = AsyncMock(
            side_effect=UpstreamUnavailableError("down", upstream="auth_api"),
        )
        h = _make_handler(client)
        await h.post("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        h.write_service_unavailable.assert_called_once()
        h._enqueue_command.assert_not_awaited()

    asyncio.run(_run())


def test_missing_challenge_id_returns_400() -> None:
    async def _run() -> None:
        client = AsyncMock()
        h = _make_handler(client, body={"challenge_id": "", "code": "123456"})
        await h.post("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        h.write_validation_error.assert_called_once()
        client.verify.assert_not_awaited()

    asyncio.run(_run())


def test_close_in_scope_returns_400() -> None:
    """close — не в OTP_REQUIRED_COMMANDS, через /confirm идти не должен."""
    async def _run() -> None:
        locker_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        client = AsyncMock()
        client.verify = AsyncMock(return_value=StepUpVerifyResult(
            scope_kind="locker_command",
            scope_payload={
                "locker_id": locker_uuid,
                "command_type": "close",
                "parameters": {},
                "max_retries": 3,
            },
        ))
        h = _make_handler(client)
        await h.post(locker_uuid)
        h.write_validation_error.assert_called_once()
        h._enqueue_command.assert_not_awaited()

    asyncio.run(_run())


def test_locker_id_mismatch_emits_security_event() -> None:
    """При mismatch locker_id публикуется command_2fa_mismatch event."""
    async def _run() -> None:
        client = AsyncMock()
        client.verify = AsyncMock(return_value=StepUpVerifyResult(
            scope_kind="locker_command",
            scope_payload={
                "locker_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "command_type": "open",
                "parameters": {},
                "max_retries": 3,
            },
        ))
        producer = AsyncMock()
        producer.publish = AsyncMock()
        h = _make_handler(client, producer=producer, body={
            "challenge_id": "ch-9999", "code": "654321",
        })
        await h.post("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        h.write_forbidden.assert_called_once()
        producer.publish.assert_awaited_once()
        kwargs = producer.publish.await_args.kwargs
        assert kwargs["event_type"] == "command_2fa_mismatch"
        assert kwargs["severity"] == "alert"
        assert kwargs["locker_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        meta = kwargs["metadata"]
        assert meta["kind"] == "locker"
        assert meta["expected"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        assert meta["actual"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        assert meta["challenge_id"] == "ch-9999"
        # OTP-код НЕ должен попадать в metadata.
        assert "code" not in meta
        import json as _json
        assert "654321" not in _json.dumps(meta)

    asyncio.run(_run())


def test_locker_id_mismatch_without_producer_does_not_crash() -> None:
    """Отсутствие producer в settings — handler не падает."""
    async def _run() -> None:
        client = AsyncMock()
        client.verify = AsyncMock(return_value=StepUpVerifyResult(
            scope_kind="locker_command",
            scope_payload={
                "locker_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "command_type": "open",
                "parameters": {},
                "max_retries": 3,
            },
        ))
        h = _make_handler(client)  # producer is None
        await h.post("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        h.write_forbidden.assert_called_once()
        h._enqueue_command.assert_not_awaited()

    asyncio.run(_run())


def test_worker_reboot_forbidden_returns_403() -> None:
    """worker не имеет права отправлять reboot даже после verify."""
    async def _run() -> None:
        locker_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        client = AsyncMock()
        client.verify = AsyncMock(return_value=StepUpVerifyResult(
            scope_kind="locker_command",
            scope_payload={
                "locker_id": locker_uuid,
                "command_type": "reboot",
                "parameters": {},
                "max_retries": 3,
            },
        ))
        h = _make_handler(client, role="worker")
        await h.post(locker_uuid)
        h.write_forbidden.assert_called_once()
        h._enqueue_command.assert_not_awaited()

    asyncio.run(_run())
