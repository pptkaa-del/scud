"""Internal handler для истории команд (запись и выборка) от backend-сервисов.

Эндпойнт `/internal/api/v1/commands/history` закрыт снаружи через Apache
(<Location /internal> Require all denied). Доступен только сервисам внутри
Docker-сети — не требует X-Remote-User.

POST идемпотентен по `command_id`: повторные POST с тем же `command_id`
сливаются с существующей записью через CommandHistoryRepository.upsert. Это
позволяет locker_api писать переходы статусов (pending → sent → executed/failed)
без координации с history_api.

GET — обобщённая выборка истории команд с фильтрами (command_type, status,
диапазон по `executed_at`, порядок). Возвращает полные строки command_history
в том же формате, что и публичный `GET /api/v1/commands/history`. Используется
reports_api для построения отчётов.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import logging

from dacite import Config

from shared.auth import ProblemDetailsHandler
from shared.dtos import PaginatedResponse

from ..history_data.db.models.command_history import CommandStatus, CommandType
from ..history_data.db.repositories.command_history_repository import (
    CommandHistoryOrder,
    CommandHistoryRepository,
)
from .dto import CommandHistoryCreateDTO
from .internal_hmac import InternalHmacMixin

logger = logging.getLogger(__name__)

_DTO_CONFIG = Config(cast=[CommandStatus, CommandType])

_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 5000


class InternalCommandHistoryHandler(InternalHmacMixin, ProblemDetailsHandler):
    """GET (выборка) / POST (upsert) /internal/api/v1/commands/history.

    POST подписан HMAC (SEC-05): тело подписывается ключом
    ``INTERNAL_HISTORY_HMAC_KEY`` и проверяется ДО парсинга/записи — наравне с
    `/events/security` и `/telemetry`. Отправитель (locker_api
    OutboundDispatcher._sign) уже подписывает все исходящие history-события,
    включая канал command_history, поэтому проверка на приёме не ломает
    happy-path. GET — выборка, подпись не требуется (только чтение).
    """

    async def get(self) -> None:
        command_type = self.get_query_argument("command_type", default=None)
        status = self.get_query_argument("status", default=None)
        executed_from_raw = self.get_query_argument("executed_from", default=None)
        executed_to_raw = self.get_query_argument("executed_to", default=None)
        order_raw = self.get_query_argument("order", default=None)

        executed_from: datetime | None = None
        executed_to: datetime | None = None
        if executed_from_raw is not None:
            try:
                executed_from = datetime.fromisoformat(executed_from_raw)
            except ValueError:
                self.write_validation_error(
                    "executed_from must be an ISO-8601 datetime",
                )
                return
        if executed_to_raw is not None:
            try:
                executed_to = datetime.fromisoformat(executed_to_raw)
            except ValueError:
                self.write_validation_error(
                    "executed_to must be an ISO-8601 datetime",
                )
                return

        order = CommandHistoryOrder.CREATED_DESC
        if order_raw is not None:
            try:
                order = CommandHistoryOrder(order_raw)
            except ValueError:
                self.write_validation_error(
                    "order must be one of: created_at_desc, executed_at_asc",
                )
                return

        limit_raw = self.get_query_argument("limit", default=str(_DEFAULT_LIMIT)) or str(_DEFAULT_LIMIT)
        offset_raw = self.get_query_argument("offset", default="0") or "0"
        try:
            limit = min(max(int(limit_raw), 1), _MAX_LIMIT)
            offset = max(int(offset_raw), 0)
        except ValueError:
            self.write_validation_error("limit and offset must be integers")
            return

        try:
            repo = CommandHistoryRepository(self.db)
            commands = await repo.get_by_filters(
                command_type=command_type,
                status=status,
                executed_from=executed_from,
                executed_to=executed_to,
                order=order,
                limit=limit,
                offset=offset,
            )
            total = await repo.get_count(
                command_type=command_type,
                status=status,
                executed_from=executed_from,
                executed_to=executed_to,
            )
            logger.info(
                "Internal: command history query returned %d/%d rows (limit=%d offset=%d)",
                len(commands), total, limit, offset,
            )
            # Сериализация идентична публичному GET /api/v1/commands/history:
            # write_json → asdict(dataclass) + datetime.isoformat (parse-able
            # через datetime.fromisoformat). Возвращаем ПОЛНЫЕ строки.
            self.write_json(PaginatedResponse(
                items=commands,
                total=total,
                limit=limit,
                offset=offset,
            ))
        except Exception as exc:
            logger.error("Internal: error querying command history: %s", exc, exc_info=True)
            self.write_internal_error(str(exc))

    async def post(self) -> None:
        if not self.verify_internal_hmac():
            logger.warning(
                "internal/api/v1/commands/history signature rejected from %s",
                self.request.remote_ip,
            )
            self.write_unauthorized("Invalid or expired signature")
            return

        # strict=False: cast-config (CommandStatus/CommandType) + forward-compat
        # с лишними ключами от продьюсера (locker_api OutboundDispatcher).
        dto = self.get_json_body_as(
            CommandHistoryCreateDTO, config=_DTO_CONFIG, strict=False,
        )

        try:
            repo = CommandHistoryRepository(self.db)
            command = await repo.upsert(
                command_id=dto.command_id,
                locker_id=dto.locker_id,
                command_type=dto.command_type,
                status=dto.status,
                locker_inventory_number=dto.locker_inventory_number,
                parameters=dto.parameters,
                created_by=dto.created_by,
                user_phone=dto.user_phone,
                user_full_name=dto.user_full_name,
                result=dto.result,
                error_message=dto.error_message,
            )
            logger.info(
                "Internal: upserted command history %s for command %s (status=%s)",
                command.id, command.command_id, command.status,
            )
            self.set_status(200)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps(asdict(command), default=str))
        except Exception as exc:
            logger.error("Internal: error upserting command history: %s", exc, exc_info=True)
            self.write_internal_error(str(exc))
