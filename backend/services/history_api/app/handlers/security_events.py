from __future__ import annotations

from datetime import datetime
import logging
import uuid

from dacite import Config
import tornado.web

from ..history_data.db.models.security_event import EventSeverity, SecurityEventType
from ..history_data.db.repositories.security_event_repository import (
    ResolveOutcome,
    SecurityEventRepository,
)
from .base import BaseHandler
from .dto import PaginatedResponse, SecurityEventCreateDTO

logger = logging.getLogger(__name__)

# Конфигурация dacite для автоматической конвертации строк в enum
_DTO_CONFIG = Config(cast=[EventSeverity, SecurityEventType])


class SecurityEventListHandler(BaseHandler):
    """
    GET/POST /api/v1/events/security - Управление событиями безопасности.

    GET: Получение списка событий с фильтрацией
    POST: Создание нового события безопасности
    """

    async def get(self) -> None:
        """Получить список событий безопасности с фильтрацией"""
        try:
            # Извлечение query параметров
            locker_id = self.get_query_argument("locker_id", default=None)
            event_type = self.get_query_argument("event_type", default=None)
            severity = self.get_query_argument("severity", default=None)
            resolved_str = self.get_query_argument("resolved", default=None)
            from_str = self.get_query_argument("from", default=None)
            to_str = self.get_query_argument("to", default=None)

            # Валидация query-фильтров ДО SQL: невалидный UUID/enum иначе уходит
            # в WHERE по uuid/enum-колонке, psycopg2 поднимает 22P02, и broad
            # except ниже маскирует это под 500 вместо корректного 400.
            if locker_id is not None:
                try:
                    uuid.UUID(locker_id)
                except ValueError:
                    self.write_validation_error(
                        f"Invalid locker_id value: {locker_id!r} (must be a UUID)"
                    )
                    return
            if event_type is not None:
                self.parse_enum_or_400(event_type, SecurityEventType, "event_type")
            if severity is not None:
                self.parse_enum_or_400(severity, EventSeverity, "severity")

            try:
                limit = int(self.get_query_argument("limit", default="100") or "100")
                offset = int(self.get_query_argument("offset", default="0") or "0")
            except ValueError:
                self.write_validation_error("limit and offset must be integers")
                return

            # Валидация limit
            if limit < 1 or limit > 10000:
                self.write_validation_error("limit must be between 1 and 10000")
                return

            # Парсинг resolved
            resolved: bool | None = None
            if resolved_str is not None:
                if resolved_str.lower() == "true":
                    resolved = True
                elif resolved_str.lower() == "false":
                    resolved = False

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
            repo = SecurityEventRepository(self.db)
            events = await repo.get_by_filters(
                locker_id=locker_id,
                event_type=event_type,
                severity=severity,
                resolved=resolved,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
                offset=offset
            )

            # Получение общего количества
            total_count = await repo.get_count(
                locker_id=locker_id,
                event_type=event_type,
                severity=severity,
                resolved=resolved,
                from_date=from_date,
                to_date=to_date
            )

            response = PaginatedResponse(
                items=events,
                total=total_count,
                limit=limit,
                offset=offset
            )
            self.write_json(response)

        except (tornado.web.Finish, tornado.web.HTTPError):
            # 400 от parse_enum_or_400/write_validation_error уже отправлен —
            # не подменяем его на 500 в broad-except ниже.
            raise
        except Exception as e:
            logger.error(f"Error fetching security events: {e}", exc_info=True)
            self.write_internal_error(str(e))

    async def post(self) -> None:
        """Создать новое событие безопасности"""
        try:
            # Получение и конвертация JSON → DTO
            # dacite автоматически конвертирует строки в enum и валидирует значения
            dto = self.get_json_body_as(SecurityEventCreateDTO, config=_DTO_CONFIG)

            # Создание события
            # dto.event_type и dto.severity уже являются enum (не строками)
            repo = SecurityEventRepository(self.db)
            event = await repo.create(
                locker_id=dto.locker_id,
                event_type=dto.event_type,
                severity=dto.severity,
                sensor_id=dto.sensor_id,
                violated_thresholds=dto.violated_thresholds,
                metadata=dto.metadata,
                event_uid=dto.event_uid,
            )

            if event is None:
                # Дубликат по event_uid (ARC-02) — идемпотентный 200.
                logger.info(
                    "Security event with event_uid=%s already exists (dedup)",
                    dto.event_uid,
                )
                self.write_json({"status": "duplicate"}, status_code=200)
                return

            logger.info(f"Created security event {event.id} for locker {event.locker_id}")

            self.write_json(event, status_code=201)

        except (tornado.web.Finish, tornado.web.HTTPError):
            # strict-валидация тела (get_json_body_as) поднимает Finish (400 уже
            # записан) — пробрасываем, иначе broad-except залогирует ложную 500.
            raise
        except Exception as e:
            logger.error(f"Error creating security event: {e}", exc_info=True)
            self.write_internal_error(str(e))


class SecurityEventDetailHandler(BaseHandler):
    """
    PATCH /api/v1/events/security/{eventId} - Закрытие события безопасности.
    """

    async def patch(self, event_id: str) -> None:
        """Закрыть (resolve) событие безопасности.

        COR-08: атрибуция (`resolved_by`) берётся из аутентифицированного
        пользователя (X-Remote-User), а НЕ из тела запроса — иначе клиент мог
        бы подделать, кто закрыл событие. Операция идемпотентна: повторное
        закрытие уже закрытого события → 409 (audit-trail не переписывается),
        несуществующего → 404.
        """
        try:
            # Преобразование event_id в int
            event_id_int = self.get_int_path_arg(event_id, "event_id")

            # Атрибуция — из аутентифицированного контекста, не из тела запроса.
            resolved_by = self.current_user_id
            if resolved_by is None:
                self.write_unauthorized("Missing authenticated user")
                return

            # Закрытие события (идемпотентно)
            repo = SecurityEventRepository(self.db)
            outcome, event = await repo.resolve_event(
                event_id=event_id_int,
                resolved_by=resolved_by,
            )

            if outcome is ResolveOutcome.NOT_FOUND:
                self.write_not_found("Security event not found")
                return

            if outcome is ResolveOutcome.ALREADY_RESOLVED:
                self.write_conflict("Security event already resolved")
                return

            logger.info(
                "Resolved security event %d by user %d", event_id_int, resolved_by
            )

            self.write_json(event)

        except (tornado.web.Finish, tornado.web.HTTPError):
            # Finish от get_int_path_arg (400 уже записан) — пробрасываем,
            # чтобы broad-except ниже не переписал его в 500.
            raise
        except Exception as e:
            logger.error(f"Error resolving security event: {e}", exc_info=True)
            self.write_internal_error(str(e))
