"""Decision table: require_2fa × command_type → issue or pass-through."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from app.handlers._step_up import Require2FAForCommandsMixin


def _make_subject(client=None):
    return Require2FAForCommandsMixin(client=client or AsyncMock())


def test_passthrough_when_require_2fa_false_open() -> None:
    async def _run() -> None:
        client = AsyncMock()
        m = _make_subject(client)
        result = await m.maybe_issue_challenge(
            user_id=1, require_2fa=False,
            locker_id="x", locker_inv="i",
            command_type="open", parameters={}, max_retries=3,
        )
        assert result is None
        client.issue.assert_not_awaited()

    asyncio.run(_run())


def test_passthrough_when_close_even_with_2fa() -> None:
    async def _run() -> None:
        client = AsyncMock()
        m = _make_subject(client)
        result = await m.maybe_issue_challenge(
            user_id=1, require_2fa=True,
            locker_id="x", locker_inv="i",
            command_type="close", parameters={}, max_retries=3,
        )
        assert result is None
        client.issue.assert_not_awaited()

    asyncio.run(_run())


def test_issue_on_open_with_2fa() -> None:
    async def _run() -> None:
        from app.services.auth_step_up_client import StepUpIssueResult
        client = AsyncMock()
        client.issue = AsyncMock(return_value=StepUpIssueResult(
            challenge_id="uuid-1", expires_at="t", delivery_status="sent",
        ))
        m = _make_subject(client)
        result = await m.maybe_issue_challenge(
            user_id=1, require_2fa=True,
            locker_id="x", locker_inv="i",
            command_type="open", parameters={"p": 1}, max_retries=3,
        )
        assert result is not None
        assert result.challenge_id == "uuid-1"
        assert result.delivery_status == "sent"
        client.issue.assert_awaited_once()
        kw = client.issue.await_args.kwargs
        assert kw["user_id"] == 1
        assert kw["scope_kind"] == "locker_command"
        assert kw["scope_payload"] == {
            "locker_id": "x", "locker_inv": "i",
            "command_type": "open", "parameters": {"p": 1}, "max_retries": 3,
        }

    asyncio.run(_run())


def test_issue_on_reboot_with_2fa() -> None:
    async def _run() -> None:
        from app.services.auth_step_up_client import StepUpIssueResult
        client = AsyncMock()
        client.issue = AsyncMock(return_value=StepUpIssueResult(
            challenge_id="uuid-2", expires_at="t", delivery_status="sent",
        ))
        m = _make_subject(client)
        result = await m.maybe_issue_challenge(
            user_id=1, require_2fa=True,
            locker_id="x", locker_inv="i",
            command_type="reboot", parameters={}, max_retries=3,
        )
        assert result is not None

    asyncio.run(_run())


def test_config_no_longer_issues_challenge() -> None:
    """`config` выведен из эксплуатации: он больше не входит в
    OTP_REQUIRED_COMMANDS, поэтому mixin не выдаёт challenge (passthrough)."""
    async def _run() -> None:
        from app.services.auth_step_up_client import StepUpIssueResult
        client = AsyncMock()
        client.issue = AsyncMock(return_value=StepUpIssueResult(
            challenge_id="uuid-3", expires_at="t", delivery_status="sent",
        ))
        m = _make_subject(client)
        result = await m.maybe_issue_challenge(
            user_id=1, require_2fa=True,
            locker_id="x", locker_inv="i",
            command_type="config", parameters={}, max_retries=3,
        )
        assert result is None
        client.issue.assert_not_awaited()

    asyncio.run(_run())
