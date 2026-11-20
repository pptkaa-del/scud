from __future__ import annotations

from datetime import datetime
import logging

from dacite import Config
import tornado.web

from ..history_data.db.models.command_history import CommandStatus, CommandType
from ..history_data.db.repositories.command_history_repository import CommandHistoryRepository
from .base import BaseHandler
from .dto import CommandHistoryCreateDTO, PaginatedResponse

logger = logging.getLogger(__name__)

# Конфигурация dacite для автоматической конвертации строк в enum
_DTO_CONFIG = Config(cast=[CommandType, CommandStatus])


class CommandHistoryHandler(BaseHandler):
    """
    GET/POST /api/v1/commands/history - Управление историей команд.

    GET: Получение истории команд с фильтрацией
    POST: Создание записи истории команды (вызывается из locker_api)
    """

    async def get(self) -> None:
        """Получить историю команд с фильтрацией"""
        try:
            # Извлечение query параметров
            locker_id = self.get_query_argument("locker_id", default=None)
            user_id_str = self.get_query_argument("user_id", default=None)
            command_type = self.get_query_argument("command_type", default=None)
            status = self.get_query_argument("status", default=None)
            from_str = self.get_query_argument("from", default=None)
            to_str = self.get_query_argument("to", default=None)
            limit = int(self.get_query_argument("limit", default="100") or "100")
            offset = int(self.get_query_argument("offset", default="0") or "0")

            # Валидация limit
            if limit < 1 or limit > 10000:
                self.write_validation_error("limit must be between 1 and 10000")
                return

            # Парсинг user_id
            user_id: int | None = None
            if user_id_str:
                try:
                    user_id = int(user_id_str)
                except ValueError:
                    self.write_validation_error("Invalid user_id format (must be integer)")
                    return

            # Парсинг дат
            from_date: datetime | None = None
            to_date: datetime | None = None

            if from_str:
                try:
                    from_date = datetime.fromisoformat(from_str)
                except ValueError:
                    self.write_validation_error("Invalid 'from' date format (use ISO 8601)")
                    return

            if to_str:
                try:
                    to_date = datetime.fromisoformat(to_str)
                except ValueError:
                    self.write_validation_error("Invalid 'to' date format (use ISO 8601)")
                    return

            # Получение данных из БД
            repo = CommandHistoryRepository(self.db)
            commands = await repo.get_by_filters(
                locker_id=locker_id,
                user_id=user_id,
                command_type=command_type,
                status=status,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
                offset=offset
            )

            # Получение общего количества
            total_count = await repo.get_count(
                locker_id=locker_id,
                user_id=user_id,
                command_type=command_type,
                status=status,
                from_date=from_date,
                to_date=to_date
            )

            response = PaginatedResponse(
                items=commands,
                total=total_count,
                limit=limit,
                offset=offset
            )
            self.write_json(response)

        except Exception as e:
            logger.error(f"Error fetching command history: {e}", exc_info=True)
            self.write_internal_error(str(e))

    async def post(self) -> None:
        """Идемпотентно создать или обновить запись истории команды.

        Эндпойнт идемпотентен по `command_id`: повторные POST с тем же
        `command_id` сливаются с существующей записью (см. CommandHistoryRepository.upsert).
        Это позволяет продьюсерам в locker_api делать ретраи без 409.
        """
        try:
            dto = self.get_json_body_as(CommandHistoryCreateDTO, config=_DTO_CONFIG)

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
                error_message=dto.error_message
            )

            logger.info(
                "Upserted command history record %s for command %s (status=%s)",
                command.id, command.command_id, command.status,
            )
            self.write_json(command, status_code=200)

        except (tornado.web.Finish, tornado.web.HTTPError):
            # get_json_body_as при strict-валидации поднимает Finish (400 уже
            # записан) — пробрасываем, иначе broad-except ниже залогирует его
            # как ложную 500 и зашумит алертинг.
            raise
        except Exception as e:
            logger.error(f"Error upserting command history: {e}", exc_info=True)
            self.write_internal_error(str(e))
