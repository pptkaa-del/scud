import logging
from typing import Any

from shared.access_control import RelayProblem, check_command_permission, is_relay_command
from shared.database import AsyncPostgres, is_unique_violation
from shared.enums import CommandTransition
from shared.http import UpstreamUnavailableError

from ..locker_data.db.models.command import CommandType
from ..locker_data.db.models.locker import Locker
from ..locker_data.db.repositories.command import CommandRepository
from ..locker_data.db.repositories.locker import LockerRepository
from ..observability import command_metrics
from ..services.command_queue import CommandQueue, dispatch_claimed
from ..services.history_producers.command_history_producer import CommandHistoryProducer
from ._relay_guard import RelayProblemResponse, check_relay_command, relay_problem
from ._step_up import Require2FAForCommandsMixin
from .base import BaseHandler
from .dto import PaginatedResponse

logger = logging.getLogger(__name__)

STEP_UP_UNAVAILABLE_DETAIL = (
    "Не удалось отправить код подтверждения. "
    "Попробуйте позже или обратитесь к администратору."
)


class CommandListHandler(BaseHandler):
    """Handler для работы с командами управления шкафом"""

    def initialize(self, db: AsyncPostgres, command_queue: CommandQueue | None = None) -> None:
        super().initialize(db)
        self.command_queue = command_queue

    async def get(self, locker_id: str) -> None:
        """Получить историю команд для шкафа с пагинацией."""
        # Defense-in-depth (SEC-02): worker видит историю только своих шкафов.
        if not self.ensure_locker_grant(locker_id):
            return

        params = self.parse_pagination_params(default_limit=100, max_limit=1000)
        if params is None:
            return
        limit, offset = params

        locker_repo = LockerRepository(self.db)
        locker = await locker_repo.get_by_id(locker_id)

        if not locker:
            self.write_not_found("Locker not found")
            return

        cmd_repo = CommandRepository(self.db)
        commands = await cmd_repo.get_commands_by_locker(locker_id, limit=limit, offset=offset)
        total = await cmd_repo.count_by_locker(locker_id)

        self.write_json(
            PaginatedResponse(items=commands, total=total, limit=limit, offset=offset)
        )

    async def post(self, locker_id: str) -> None:
        """Создать новую команду для шкафа.

        Если у пользователя ``X-Remote-Require-2FA: true`` и command_type
        входит в ``OTP_REQUIRED_COMMANDS`` (open/reboot), вместо
        записи команды возвращаем 202 + challenge_id; клиент должен прислать
        код на ``POST /commands/confirm``.
        """
        body = self.get_json_body()

        type_str = body.get("type")
        if not type_str:
            self.write_validation_error("type is required")
            return

        try:
            command_type = CommandType(type_str)
        except ValueError:
            self.write_validation_error(f"Invalid command type: {type_str}")
            return

        # Тонкая проверка command_type против X-Remote-Role.
        # ACM URL-level rule (acm/rules.py) пропускает worker+Допуск, но
        # запрещает worker'у отправлять REBOOT (CONFIG выведен из эксплуатации
        # для всех ролей). Default-deny если
        # X-Remote-Role не выставлен (например при misconfigured gateway).
        # Permission ДО locker existence check — не раскрываем существование
        # шкафов пользователям без прав на команды.
        # `.lower()` defense-in-depth: ACM сейчас отдаёт lowercase значение
        # (StrEnum value), но если в будущем gateway/issuer начнёт писать
        # «Worker» или «WORKER» — мы не будем молча отказывать
        # легитимным worker'ам в `open`/`close`. Матрица всё равно
        # хранит только lowercase ключи.
        role = self.remote_role.strip().lower()
        if not check_command_permission(role, command_type.value):
            logger.warning(
                "Command permission denied: user_id=%s role=%r tried %s on locker=%s",
                self.current_user_id, role, command_type.value, locker_id,
            )
            self.write_forbidden(
                f"Role does not permit '{command_type.value}' command"
            )
            return

        # Defense-in-depth (SEC-02): worker должен иметь Допуск на этот шкаф.
        # Проверка ДО locker existence check — не раскрываем существование
        # шкафов worker'ам без Допуска и не доверяем ACM слепо.
        if not self.ensure_locker_grant(locker_id):
            return

        parameters = body.get("parameters", {})
        max_retries = body.get("max_retries", 3)

        # Проверяем существование шкафа
        locker_repo = LockerRepository(self.db)
        locker = await locker_repo.get_by_id(locker_id)

        if not locker:
            self.write_not_found("Locker not found")
            return

        if is_relay_command(command_type):
            relay_parameters = await self._checked_relay_parameters(locker, command_type, parameters)
            if relay_parameters is None:
                return
            parameters = relay_parameters

        # Step-up 2FA decision: если у пользователя require_2fa=true и
        # command_type требует второго фактора — выдаём challenge вместо
        # выполнения команды. Иначе path остаётся побайтово прежним.
        require_2fa = (
            self.request.headers.get("X-Remote-Require-2FA", "false")
            .strip()
            .lower()
            == "true"
        )
        mixin: Require2FAForCommandsMixin = self.settings["step_up_mixin"]
        try:
            challenge = await mixin.maybe_issue_challenge(
                user_id=self.current_user_id or 0,
                require_2fa=require_2fa,
                locker_id=locker_id,
                locker_inv=locker.inventory_number,
                command_type=command_type.value,
                parameters=parameters,
                max_retries=max_retries,
            )
        except UpstreamUnavailableError:
            self.write_service_unavailable(STEP_UP_UNAVAILABLE_DETAIL)
            return

        if challenge is not None:
            if challenge.challenge_id is None:
                # Нет ни телефона, ни Max-привязки — доставить код 2FA некуда (защитный крайний случай).
                self.write_problem(
                    type_uri="https://api.scuddocs.com/problems/max-binding-required",
                    title="Max binding required",
                    status=401,
                    detail=(
                        "Не удалось доставить код подтверждения второго фактора. "
                        "Обратитесь к администратору."
                    ),
                )
                return
            self.write_json(
                {
                    "challenge_id": challenge.challenge_id,
                    "expires_at": challenge.expires_at,
                    "delivery_status": challenge.delivery_status,
                },
                status_code=202,
            )
            return

        await self._enqueue_command(locker, command_type, parameters, max_retries)

    def write_relay_problem(self, problem: RelayProblemResponse) -> None:
        self.write_problem(
            type_uri=problem.type_uri, title=problem.title,
            status=problem.status, detail=problem.detail,
        )

    async def _checked_relay_parameters(
        self, locker: Locker, command_type: CommandType, parameters: Any,
    ) -> dict[str, Any] | None:
        """Нормализованные parameters relay-команды; None — отказ уже записан."""
        result = await check_relay_command(
            self.db,
            role=self.remote_role.strip().lower(),
            locker=locker,
            command_type=command_type,
            parameters=parameters,
        )
        if isinstance(result, RelayProblemResponse):
            logger.info(
                "Relay command rejected: user_id=%s locker=%s type=%s problem=%s",
                self.current_user_id, locker.id, command_type.value, result.type_uri,
            )
            self.write_relay_problem(result)
            return None
        return {"relay": result.relay}

    async def _enqueue_command(
        self,
        locker: Locker,
        command_type: CommandType,
        parameters: dict[str, Any],
        max_retries: int,
    ) -> None:
        """Запись pending команды + history + немедленная доставка по WS.

        Это исходное тело post() для не-2FA flow — выделено в helper, чтобы
        и ``CommandConfirmHandler`` мог переиспользовать тот же путь после
        successful verify.
        """
        locker_id = locker.id

        # Создаем команду
        cmd_repo = CommandRepository(self.db)
        try:
            command = await cmd_repo.create(
                locker_id=locker_id,
                command_type=command_type,
                created_by=self.current_user_id,
                parameters=parameters,
                max_retries=max_retries,
            )
        except Exception as exc:
            # Частичный уникальный индекс: одна активная relay-команда на канал.
            if is_relay_command(command_type) and is_unique_violation(exc):
                self.write_relay_problem(relay_problem(
                    RelayProblem.COMMAND_IN_PROGRESS,
                    f"Relay {parameters.get('relay')} already has an active command",
                ))
                return
            raise
        command_metrics.record_transition(command.type, CommandTransition.QUEUED)

        # Запись pending в history (UI видит очередь и для offline-локеров)
        history_producer = CommandHistoryProducer(self.db)
        try:
            await history_producer.publish_created(
                command_id=command.id,
                locker_id=locker_id,
                command_type=command.type,
                parameters=command.parameters,
                created_by=command.created_by,
                locker_inventory_number=locker.inventory_number,
            )
        except Exception as exc:
            logger.error("Failed to enqueue command_history pending for %s: %s", command.id, exc)

        # Немедленная доставка если устройство уже подключено
        if self.command_queue and self.command_queue.is_connected(locker_id):
            # CLAIM-before-send (COR-02/18): сначала заявляем команду
            # pending→sent через conditional CAS, и только при успехе
            # отправляем. CAS гарантирует at-most-once доставку: если
            # фоновый queue-processor успел заявить ту же команду раньше,
            # claim вернёт None — мы НЕ отправим её повторно (no double-send),
            # а уже выполненную (executed) команду claim тоже не откатит.
            claimed = await cmd_repo.update_status_to_sent(
                command.id, ttl_s=self.command_queue.command_ttl_s,
            )
            if claimed is not None and await dispatch_claimed(
                self.command_queue, claimed, cmd_repo,
            ):
                logger.info("Command %s immediately dispatched to connected locker %s", command.id, locker_id)
                try:
                    await history_producer.publish_sent(
                        command_id=command.id,
                        locker_id=locker_id,
                        command_type=command.type,
                        parameters=command.parameters,
                        created_by=command.created_by,
                        locker_inventory_number=locker.inventory_number,
                    )
                except Exception as exc:
                    logger.error("Failed to enqueue command_history sent for %s: %s", command.id, exc)

        self.write_json(command, status_code=201)


class CommandDetailHandler(BaseHandler):
    """Handler для работы с конкретной командой"""

    async def get(self, locker_id: str, command_id: str) -> None:
        """Получить информацию о команде"""
        cmd_repo = CommandRepository(self.db)
        command = await cmd_repo.get_by_id(command_id)

        if not command or command.locker_id != locker_id:
            self.write_not_found("Command not found")
            return

        self.write_json(command)
