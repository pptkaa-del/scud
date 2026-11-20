"""POST /api/v1/locker/{locker_id}/commands/confirm — step-up подтверждение."""
from __future__ import annotations

import logging
from typing import Any

from shared.access_control import OTP_REQUIRED_COMMANDS, check_command_permission, is_relay_command
from shared.http import UpstreamUnavailableError
from shared.metrics import command_2fa_challenges_total

from ..locker_data.db.models.command import CommandType
from ..locker_data.db.repositories.locker import LockerRepository
from ..services.auth_step_up_client import (
    AuthStepUpClient,
    StepUpVerifyError,
)
from ..services.history_producers.step_up_event_producer import StepUpEventProducer
from .command import CommandListHandler

logger = logging.getLogger(__name__)

_EVT_MISMATCH = "command_2fa_mismatch"
_SEV_ALERT = "alert"
STEP_UP_VERIFY_UNAVAILABLE_DETAIL = (
    "Не удалось проверить код подтверждения. Попробуйте позже."
)


class CommandConfirmHandler(CommandListHandler):
    """POST /api/v1/locker/{id}/commands/confirm — verify code + enqueue.

    Наследуется от ``CommandListHandler`` чтобы переиспользовать
    ``_enqueue_command``. Path-параметр в POST совпадает по сигнатуре
    с родителем, mypy override-warning не будет.

    Важно: ``get()`` наследуется от ``CommandListHandler`` и Tornado диспатчит
    по HTTP-методу, поэтому GET на ``/commands/confirm`` достижим и вернёт
    список команд локера (как у родителя), а не 405. Если такое поведение
    нежелательно — переопределите ``get()`` явным 405.
    """

    async def post(self, locker_id: str) -> None:
        body = self.get_json_body()
        challenge_id_raw = body.get("challenge_id") or ""
        code_raw = body.get("code") or ""
        challenge_id = str(challenge_id_raw).strip()
        code = str(code_raw).strip()
        if not challenge_id or not code:
            self.write_validation_error("challenge_id and code are required")
            return

        client: AuthStepUpClient = self.settings["auth_step_up_client"]
        try:
            verify = await client.verify(
                challenge_id=challenge_id,
                user_id=self.current_user_id or 0,
                code=code,
            )
        except UpstreamUnavailableError:
            self.write_service_unavailable(STEP_UP_VERIFY_UNAVAILABLE_DETAIL)
            return

        if isinstance(verify, StepUpVerifyError):
            if verify.status_code == 403:
                self.write_forbidden(verify.detail or "Challenge rejected")
            else:
                self.write_unauthorized(verify.detail or "Invalid or expired code")
            return

        # Union[StepUpVerifyResult, StepUpVerifyError] — после early-return
        # на StepUpVerifyError mypy сужает тип до StepUpVerifyResult.
        if verify.scope_kind != "locker_command":
            self.write_forbidden("Challenge scope does not match command")
            return

        payload = verify.scope_payload
        expected_locker_raw = payload.get("locker_id", "")
        expected_locker = str(expected_locker_raw) if expected_locker_raw else ""
        if expected_locker != locker_id:
            # Воспроизвести challenge на чужом шкафе — потенциальная атака.
            # Логируем, отдаём 403, публикуем security event для аудита.
            logger.warning(
                "command_2fa_mismatch: user_id=%s expected_locker=%s actual=%s",
                self.current_user_id, expected_locker, locker_id,
            )
            command_2fa_challenges_total.labels(outcome="mismatch").inc()
            await self._publish_mismatch_event(
                challenge_id=challenge_id,
                locker_id=locker_id,
                metadata={
                    "user_id": self.current_user_id,
                    "challenge_id": challenge_id,
                    "kind": "locker",
                    "expected": expected_locker,
                    "actual": locker_id,
                },
            )
            self.write_forbidden("Challenge locker does not match URL")
            return

        cmd_type_str = str(payload.get("command_type", ""))
        try:
            command_type = CommandType(cmd_type_str)
        except ValueError:
            self.write_forbidden("Invalid command type in challenge")
            return

        if cmd_type_str not in OTP_REQUIRED_COMMANDS:
            # close попал в /confirm? Это не ошибка авторизации, но flow
            # неконсистентен — close идёт через /commands напрямую.
            self.write_validation_error(
                f"{cmd_type_str} does not require step-up confirmation"
            )
            return

        role = self.remote_role.strip().lower()
        if not check_command_permission(role, cmd_type_str):
            self.write_forbidden(
                f"Role does not permit '{cmd_type_str}' command"
            )
            return

        locker_repo = LockerRepository(self.db)
        locker = await locker_repo.get_by_id(locker_id)
        if locker is None:
            self.write_not_found("Locker not found")
            return

        parameters_raw = payload.get("parameters", {})
        parameters = parameters_raw if isinstance(parameters_raw, dict) else {}
        if is_relay_command(command_type):
            # Политика канала могла измениться за время ввода кода.
            relay_parameters = await self._checked_relay_parameters(
                locker, command_type, parameters_raw,
            )
            if relay_parameters is None:
                return
            parameters = relay_parameters
        max_retries_raw = payload.get("max_retries", 3)
        max_retries = max_retries_raw if isinstance(max_retries_raw, int) else 3

        await self._enqueue_command(locker, command_type, parameters, max_retries)

    async def _publish_mismatch_event(
        self,
        *,
        challenge_id: str,
        locker_id: str,
        metadata: dict[str, Any],
    ) -> None:
        """Опубликовать ``command_2fa_mismatch`` event в history_api.

        Никогда не падает наружу — пропавший аудит не должен превращать
        403 в 500. Продьюсер сам глушит сетевые ошибки.
        """
        producer: StepUpEventProducer | None = self.settings.get(
            "security_event_producer",
        )
        if producer is None:
            return
        try:
            await producer.publish(
                event_type=_EVT_MISMATCH,
                severity=_SEV_ALERT,
                locker_id=locker_id,
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning(
                "command_2fa_mismatch event emission failed (challenge_id=%s): %s",
                challenge_id, exc,
            )
