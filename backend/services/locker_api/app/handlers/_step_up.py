"""Pre-handler mixin: решает, нужен ли OTP-челлендж для команды.

Решающая логика выделена из handler'а, чтобы тестировалась чистая
Python-функция без Tornado-обвязки. Использует ``OTP_REQUIRED_COMMANDS`` из
shared (open/reboot) — close отдаётся без второго фактора.
"""
from __future__ import annotations

from typing import Any

from shared.access_control.commands import OTP_REQUIRED_COMMANDS

from ..services.auth_step_up_client import AuthStepUpClient, StepUpIssueResult


class Require2FAForCommandsMixin:
    """Pre-handler решение: issue или pass-through на enqueue.

    Использование (см. ``CommandListHandler.post``)::

        result = await mixin.maybe_issue_challenge(...)
        if result is None: enqueue_command(...)              # обычный 201
        elif result.challenge_id is None: 401 max-binding     # 2FA on, Max not bound
        else: 202 + challenge_id                              # ждём confirm
    """

    def __init__(self, client: AuthStepUpClient) -> None:
        self._client = client

    async def maybe_issue_challenge(
        self,
        *,
        user_id: int,
        require_2fa: bool,
        locker_id: str,
        locker_inv: str,
        command_type: str,
        parameters: dict[str, Any],
        max_retries: int,
    ) -> StepUpIssueResult | None:
        """Если 2FA включён и команда привилегированная — issue, иначе None."""
        if not require_2fa:
            return None
        if command_type not in OTP_REQUIRED_COMMANDS:
            return None
        return await self._client.issue(
            user_id=user_id,
            scope_kind="locker_command",
            scope_payload={
                "locker_id": locker_id,
                "locker_inv": locker_inv,
                "command_type": command_type,
                "parameters": parameters,
                "max_retries": max_retries,
            },
            ttl=300,
        )
