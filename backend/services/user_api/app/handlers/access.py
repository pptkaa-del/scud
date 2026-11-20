"""HTTP обработчики для модуля управления допусками пользователей"""
import logging

from app.handlers.base import BaseHandler
from app.handlers.dto import (
    BatchAccessRequestDTO,
    CreateUserAccessDTO,
    PaginatedResponse,
    UpdateUserAccessDTO,
)
from app.services.cache_invalidator import CacheInvalidator
from app.user_data.db.repositories.access import AccessRepository
from app.user_data.db.repositories.user import UserRepository

logger = logging.getLogger(__name__)


def _get_invalidator(handler: BaseHandler) -> CacheInvalidator | None:
    """Достаёт CacheInvalidator из app settings (может отсутствовать в unit-тестах)."""
    return handler.settings.get("cache_invalidator")


async def _invalidate_user_ctx(handler: BaseHandler, user_id: int) -> None:
    """Инвалидирует ACM-кэш контекста пользователя после изменения допуска.

    Best-effort: ошибки Redis уже проглатываются внутри CacheInvalidator —
    провал инвалидации не должен ломать мутацию допуска (как и при role-change).
    """
    invalidator = _get_invalidator(handler)
    if invalidator is not None:
        await invalidator.invalidate_user(user_id)


class UserAccessListHandler(BaseHandler):
    """Обработчик для списка допусков пользователя (GET/POST /users/{user_id}/accesses)"""

    async def get(self, user_id: str) -> None:
        """
        Получение списка допусков пользователя с пагинацией.

        Query параметры:
            include_inactive: включать ли деактивированные допуски (boolean)
            limit, offset: пагинация
        """
        user_id_int = self.get_int_path_arg(user_id, "user_id")

        user_repo = UserRepository(self.db)
        user = await user_repo.get_by_id(user_id_int)
        if not user:
            self.write_not_found(f"User with ID {user_id_int} not found")
            return

        include_inactive_str = self.get_query_argument("include_inactive", default="false") or "false"
        include_inactive = include_inactive_str.lower() in ["true", "1", "yes"]

        params = self.parse_pagination_params(default_limit=100, max_limit=1000)
        if params is None:
            return
        limit, offset = params

        repo = AccessRepository(self.db)
        accesses = await repo.get_all_for_user(
            user_id_int,
            include_inactive=include_inactive,
            limit=limit,
            offset=offset,
        )
        total = await repo.count_for_user(user_id_int, include_inactive=include_inactive)

        self.write_json(
            PaginatedResponse(items=accesses, total=total, limit=limit, offset=offset)
        )

    async def post(self, user_id: str) -> None:
        """
        Создание нового допуска для пользователя

        Request body:
            start_at: дата начала действия допуска (обязательно)
            end_at: дата окончания действия допуска (опционально)
            request_id: ID заявки на доступ (опционально)
            full_access: полный доступ ко всем шкафам (обязательно)
            locker_ids: список ID шкафов (обязательно если full_access=false)
        """
        if not self.current_user:
            self.write_unauthorized("User authentication required")
            return

        user_id_int = self.get_int_path_arg(user_id, "user_id")

        # Проверяем существование пользователя
        user_repo = UserRepository(self.db)
        user = await user_repo.get_by_id(user_id_int)
        if not user:
            self.write_not_found(f"User with ID {user_id_int} not found")
            return

        # Получение и конвертация JSON → DTO с автоматической валидацией
        dto = self.get_json_body_as(CreateUserAccessDTO)

        # Создаем допуск
        repo = AccessRepository(self.db)
        access = await repo.create(
            user_id=user_id_int,
            start_at=dto.start_at,
            end_at=dto.end_at,
            granted_by=self.current_user.id,
            request_id=dto.request_id,
            full_access=dto.full_access,
            locker_ids=dto.locker_ids if not dto.full_access else None,
        )
        await user_repo.refresh_expire_date(user_id_int)

        # Новый допуск меняет ACM-контекст пользователя — инвалидируем кэш,
        # иначе допуск «виден» только после истечения TTL.
        await _invalidate_user_ctx(self, user_id_int)

        self.write_json(access, status_code=201)


class UserAccessDetailHandler(BaseHandler):
    """Обработчик для конкретного допуска (GET/PATCH/DELETE /accesses/{access_id})"""

    async def get(self, access_id: str) -> None:
        """Получение допуска по ID"""
        access_id_int = self.get_int_path_arg(access_id, "access_id")

        repo = AccessRepository(self.db)
        access = await repo.get_by_id(access_id_int)

        if not access:
            self.write_not_found(f"Access with ID {access_id_int} not found")
            return

        self.write_json(access)

    async def patch(self, access_id: str) -> None:
        """
        Обновление допуска (продление срока или изменение списка шкафов)

        Request body (все поля опциональны):
            start_at: новая дата начала действия
            end_at: новая дата окончания действия
            locker_ids: новый список ID шкафов
        """
        access_id_int = self.get_int_path_arg(access_id, "access_id")

        # Получение и конвертация JSON → DTO с автоматической валидацией
        dto = self.get_json_body_as(UpdateUserAccessDTO)

        # Обновляем допуск
        repo = AccessRepository(self.db)
        access = await repo.update(
            access_id=access_id_int,
            start_at=dto.start_at,
            end_at=dto.end_at,
            locker_ids=dto.locker_ids,
        )

        if not access:
            self.write_not_found(f"Access with ID {access_id_int} not found")
            return

        user_repo = UserRepository(self.db)
        await user_repo.refresh_expire_date(access.user_id)

        # Изменение срока/списка шкафов меняет ACM-контекст — инвалидируем кэш.
        await _invalidate_user_ctx(self, access.user_id)

        self.write_json(access)

    async def delete(self, access_id: str) -> None:
        """Деактивация допуска (is_active=false)"""
        access_id_int = self.get_int_path_arg(access_id, "access_id")

        repo = AccessRepository(self.db)
        access = await repo.deactivate(access_id_int)

        if not access:
            self.write_not_found(f"Access with ID {access_id_int} not found")
            return

        user_repo = UserRepository(self.db)
        await user_repo.refresh_expire_date(access.user_id)

        # Отозванный допуск должен перестать авторизовывать немедленно (SEC-15).
        await _invalidate_user_ctx(self, access.user_id)

        self.set_status(204)


class UserAccessBatchHandler(BaseHandler):
    """Обработчик батч-запроса допусков по нескольким пользователям (POST /accesses/batch)"""

    async def post(self) -> None:
        """
        Получение допусков для нескольких пользователей за один запрос.

        Request body:
            user_ids: список ID пользователей (обязательно, 1–100 элементов)
            include_inactive: включать ли деактивированные допуски (по умолчанию false)

        Response 200:
            items: словарь {user_id: [допуски]}; пользователи без допусков → пустой список
        """
        dto = self.get_json_body_as(BatchAccessRequestDTO)

        repo = AccessRepository(self.db)
        result = await repo.get_all_for_users(dto.user_ids, dto.include_inactive)

        self.write_json({"items": {str(uid): accesses for uid, accesses in result.items()}})


class UserAccessReactivateHandler(BaseHandler):
    """Обработчик для реактивации допуска (POST /accesses/{access_id}/reactivate)"""

    async def post(self, access_id: str) -> None:
        """Реактивация ранее деактивированного допуска"""
        access_id_int = self.get_int_path_arg(access_id, "access_id")

        repo = AccessRepository(self.db)
        access = await repo.reactivate(access_id_int)

        if not access:
            self.write_not_found(f"Access with ID {access_id_int} not found")
            return

        user_repo = UserRepository(self.db)
        await user_repo.refresh_expire_date(access.user_id)

        # Реактивированный допуск снова авторизует — инвалидируем ACM-кэш.
        await _invalidate_user_ctx(self, access.user_id)

        self.write_json(access)
