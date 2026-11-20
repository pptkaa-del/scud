"""HTTP обработчики для модуля работы с заявками на экстренное открытие шкафов"""
import logging
import uuid

from dacite import Config

from app.handlers.base import BaseHandler, caller_role
from app.handlers.dto import CreateEmergencyRequestDTO
from app.user_data.db.models.emergency_request import EmergencyRequestStatus
from app.user_data.db.repositories.access import AccessRepository
from app.user_data.db.repositories.emergency_request import EmergencyRequestRepository
from shared.enums import UserRole

logger = logging.getLogger(__name__)

# Конфигурация dacite для строгой проверки типов
_DTO_CONFIG = Config(strict=True, check_types=True)


def _is_worker(handler: BaseHandler) -> bool:
    """True если роль текущего пользователя — worker.

    Использует общий ``caller_role`` (роль из ``current_user``, загружен из БД
    по доверенному X-Remote-User), как и privilege-guard в ``handlers/user.py``
    (SEC-01/03). worker ограничен Допуском (SEC-08) и видит только свои заявки
    (SEC-09); operator и administrator не ограничены. ``caller_role`` вернёт
    ``None`` при отсутствии пользователя — тогда worker-ность False.
    """
    return caller_role(handler) == UserRole.WORKER.value


class EmergencyRequestListHandler(BaseHandler):
    """Обработчик для списка заявок (GET /emergency-requests, POST /emergency-requests)"""

    async def get(self) -> None:
        """
        Получение списка заявок с фильтрацией и пагинацией

        Query параметры:
            status: фильтр по статусу (pending/closed)
            locker_id: фильтр по ID шкафа
            limit: количество записей (по умолчанию 100)
            offset: смещение для пагинации (по умолчанию 0)
        """
        # Получаем query параметры
        status_str = self.get_query_argument("status", default=None)
        locker_id_str = self.get_query_argument("locker_id", default=None)
        limit_str = self.get_query_argument("limit", default="100") or "100"
        offset_str = self.get_query_argument("offset", default="0") or "0"

        # Парсим и валидируем параметры
        status = None
        if status_str:
            try:
                status = EmergencyRequestStatus(status_str)
            except ValueError:
                self.write_validation_error("Invalid status parameter. Must be 'pending' or 'closed'")
                return

        locker_id = None
        if locker_id_str:
            try:
                uuid.UUID(locker_id_str)
                locker_id = locker_id_str
            except ValueError:
                self.write_validation_error("Invalid locker_id parameter. Must be a valid UUID")
                return

        try:
            limit = int(limit_str)
            offset = int(offset_str)
            if limit <= 0 or limit > 1000:
                raise ValueError("limit must be between 1 and 1000")
            if offset < 0:
                raise ValueError("offset must be non-negative")
        except ValueError as e:
            self.write_validation_error(f"Invalid pagination parameters: {e!s}")
            return

        # SEC-09: worker видит только свои заявки. Фильтр пушится в SQL —
        # иначе total из count() утёк бы количество чужих заявок.
        # _is_worker → True гарантирует наличие current_user (роль берётся из БД).
        user = self.current_user
        scope_user_id = user.id if user is not None and _is_worker(self) else None

        # Получаем список заявок
        repo = EmergencyRequestRepository(self.db)
        requests = await repo.get_all(
            status=status,
            locker_id=locker_id,
            user_id=scope_user_id,
            limit=limit,
            offset=offset
        )

        # Опционально получаем общее количество для пагинации
        total = await repo.count(status=status, locker_id=locker_id, user_id=scope_user_id)

        # Возвращаем результат с метаданными
        self.write_json({
            "items": requests,
            "total": total,
            "limit": limit,
            "offset": offset
        })

    async def post(self) -> None:
        """
        Создание новой заявки на экстренное открытие

        Request body:
            locker_id: ID шкафа (обязательно)
        """
        if not self.current_user:
            self.write_unauthorized("User authentication required")
            return

        # Получение и конвертация JSON → DTO с автоматической валидацией
        dto = self.get_json_body_as(CreateEmergencyRequestDTO, config=_DTO_CONFIG)

        # SEC-08: worker вправе подать заявку только на шкаф, на который у него
        # есть активный Допуск. Документированный контроль (см. acm/app/rules.py)
        # живёт здесь, в user_api. operator/administrator не ограничены.
        if _is_worker(self):
            access_repo = AccessRepository(self.db)
            if not await access_repo.has_active_grant(self.current_user.id, dto.locker_id):
                logger.warning(
                    "Emergency-request grant guard: user_id=%s denied for locker %s "
                    "(no active access grant)",
                    self.current_user.id, dto.locker_id,
                )
                self.write_forbidden(
                    "No active access grant for the specified locker"
                )
                return

        # Создаем заявку
        repo = EmergencyRequestRepository(self.db)
        request = await repo.create(
            user_id=self.current_user.id,
            locker_id=dto.locker_id
        )

        # Возвращаем детальную информацию о созданной заявке
        request_with_details = await repo.get_by_id(request.id)

        self.write_json(request_with_details, status_code=201)


class EmergencyRequestDetailHandler(BaseHandler):
    """Обработчик для конкретной заявки (GET/DELETE /emergency-request/{id})"""

    async def get(self, request_id: str) -> None:
        """
        Получение детальной информации о заявке

        Автоматически логирует просмотр заявки.
        """
        req_id = self.get_int_path_arg(request_id, "request_id")

        repo = EmergencyRequestRepository(self.db)
        # Сначала выбираем БЕЗ логирования просмотра — чтобы доступ к чужой
        # заявке (SEC-09) не оставил аудит-следа до проверки прав.
        request = await repo.get_by_id(req_id)

        if not request:
            self.write_not_found(f"Emergency request with ID {req_id} not found")
            return

        # SEC-09: worker может читать только свои заявки. Чужую — 404 (не 403),
        # чтобы не раскрывать факт существования заявки на конкретный шкаф.
        # _is_worker → True гарантирует наличие current_user (роль берётся из БД).
        user = self.current_user
        if user is not None and _is_worker(self) and request.user_id != user.id:
            self.write_not_found(f"Emergency request with ID {req_id} not found")
            return

        # Право подтверждено — фиксируем просмотр в аудит-логе.
        if self.current_user is not None:
            await repo.log_view(req_id, self.current_user.id)

        self.write_json(request)

    async def delete(self, request_id: str) -> None:
        """
        Закрытие заявки (только для операторов/администраторов)

        Заявка не удаляется физически, а меняет статус на 'closed'.
        """
        if not self.current_user:
            self.write_unauthorized("User authentication required")
            return

        req_id = self.get_int_path_arg(request_id, "request_id")

        repo = EmergencyRequestRepository(self.db)

        # Закрываем заявку
        request = await repo.close(
            request_id=req_id,
            closed_by=self.current_user.id
        )

        if not request:
            # Проверяем, существует ли заявка вообще
            existing = await repo.get_by_id(req_id)

            if not existing:
                self.write_not_found(f"Emergency request with ID {req_id} not found")
                return

            # Заявка существует, но уже закрыта
            self.write_conflict("Request already closed. This request has already been closed")
            return

        # Успешно закрыта
        self.set_status(204)


class EmergencyRequestLogsHandler(BaseHandler):
    """Обработчик для получения логов действий с заявкой"""

    async def get(self, request_id: str) -> None:
        """
        Получение истории действий с заявкой

        Возвращает список всех действий (created, closed, viewed) с временными метками.
        """
        req_id = self.get_int_path_arg(request_id, "request_id")

        repo = EmergencyRequestRepository(self.db)

        # Проверяем существование заявки
        request = await repo.get_by_id(req_id)
        if not request:
            self.write_not_found(f"Emergency request with ID {req_id} not found")
            return

        # Получаем логи
        logs = await repo.get_logs(req_id)

        self.write_json(logs)
