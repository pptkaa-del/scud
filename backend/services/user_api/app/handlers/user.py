from dataclasses import asdict
import hashlib
import logging
from urllib.parse import quote as _urlquote

from dacite import Config

from app.handlers.base import BaseHandler, caller_role
from app.handlers.dto import CreateUserDTO, PaginatedResponse, UpdateUserDTO
from app.services.auth_session_client import AuthApiSessionClient
from app.services.cache_invalidator import CacheInvalidator
from app.user_data.db.models.user import UserRole, UserState
from app.user_data.db.repositories.max_invite_token import MaxInviteTokenRepository
from app.user_data.db.repositories.user import UserRepository
from shared.access_control import (
    can_assign_role,
    can_modify_target,
)
from shared.database import is_unique_violation
from shared.search import normalize_query
from shared.utils import hash_password

logger = logging.getLogger(__name__)

# Конфигурация dacite для автоматической конвертации строк в enum
_DTO_CONFIG = Config(cast=[UserRole])

# Длина SHA-256 префикса, логируемого вместо сырого номера телефона (PII-redaction).
# Изменение этой константы автоматически отражается в тестах — bare literal недопустим.
_PHONE_HASH_PREFIX_LEN: int = 8


def _redact_phone(phone: str) -> str:
    """Возвращает первые _PHONE_HASH_PREFIX_LEN символов SHA-256 от телефона.

    Используется только для логирования — идентифицирует гонку без утечки PII.
    """
    return hashlib.sha256(phone.encode()).hexdigest()[:_PHONE_HASH_PREFIX_LEN]

# Максимальная длина поискового запроса `q` — соответствует VARCHAR(255)
# колонки users.full_name (см. миграцию 001_init.sql).
_Q_MAX_LENGTH = 255


def _get_invalidator(handler: BaseHandler) -> CacheInvalidator | None:
    """Достаёт CacheInvalidator из app settings (может отсутствовать в unit-тестах)."""
    return handler.settings.get("cache_invalidator")


async def _revoke_sessions(handler: BaseHandler, user_id: int) -> None:
    """Best-effort отзыв всех живых сессий пользователя в auth_api (SEC-14).

    Дёргается ПОСЛЕ успешного коммита смены учётных данных (пароль / 2FA).
    Если auth_api недоступен — логируем WARNING (без секретов/токенов) и
    продолжаем: смена учётных данных уже произошла и НЕ должна провалиться
    из-за недоступности auth_api. Клиент может отсутствовать в settings
    (unit-тесты, не передающие его) — тогда тихо выходим.
    """
    client: AuthApiSessionClient | None = handler.settings.get("auth_session_client")
    if client is None:
        return
    try:
        await client.revoke_user_sessions(user_id)
    except Exception:
        logger.warning(
            "session revoke after credential change failed for user_id=%s",
            user_id,
            exc_info=True,
        )


class UserHandler(BaseHandler):
    async def get(self, user_id: str) -> None:
        """Получение пользователя по ID"""
        user_id_int = self.get_int_path_arg(user_id, "user_id")

        repo = UserRepository(self.db)
        user = await repo.get_by_id(user_id_int)

        if not user:
            self.write_not_found(f"User with ID {user_id_int} not found")
            return

        self.write_json(user)

    async def patch(self, user_id: str) -> None:
        """Частичное обновление пользователя"""
        user_id_int = self.get_int_path_arg(user_id, "user_id")

        if not self.current_user:
            self.write_unauthorized("User authentication required")
            return

        dto = self.get_json_body_as(UpdateUserDTO, config=_DTO_CONFIG)

        if not dto.has_changes():
            self.write_validation_error("Request body must contain at least one field to update")
            return

        repo = UserRepository(self.db)

        # Проверяем, что целевой пользователь существует
        target = await repo.get_by_id(user_id_int)
        if not target:
            self.write_not_found(f"User with ID {user_id_int} not found")
            return

        # Защита от эскалации привилегий (SEC-01/03). Меняем role / password
        # только если caller вправе трогать данного target'а и назначаемую роль.
        # Изменение не-привилегированных полей (phone/full_name/contractor_id)
        # допускается без этой проверки — ACM URL-rule уже пропустил operator+.
        caller = caller_role(self)
        touches_privileged = dto.role is not None or dto.password is not None
        if touches_privileged:
            # can_modify_target требует СТРОГО более низкую роль target'а
            # (operator → worker, не operator → operator), поэтому отдельная
            # проверка self-promotion здесь избыточна: self-modification
            # привилегированных полей уже отклоняется (caller и target делят
            # одну роль → target_rank < caller_rank ложно → 403).
            if not can_modify_target(caller, target.role.value):
                logger.warning(
                    "Privilege guard: user_id=%s role=%r denied modify of "
                    "target=%s (role=%s)",
                    self.current_user.id, caller, user_id_int,
                    target.role.value,
                )
                self.write_forbidden(
                    "Insufficient privileges to modify this user"
                )
                return
            if dto.role is not None and not can_assign_role(
                caller, dto.role.value
            ):
                logger.warning(
                    "Privilege guard: user_id=%s role=%r cannot assign "
                    "role=%s",
                    self.current_user.id, caller, dto.role.value,
                )
                self.write_forbidden(
                    "Cannot assign a role higher than your own"
                )
                return

        # Если меняется телефон — проверяем уникальность
        if dto.phone is not None and dto.phone != target.phone:
            existing = await repo.get_by_phone(dto.phone)
            if existing:
                self.write_conflict(f"User with phone {dto.phone} already exists")
                return

        updated = await repo.update(
            user_id=user_id_int,
            updated_by=self.current_user.id,
            phone=dto.phone,
            full_name=dto.full_name,
            role=dto.role,
            contractor_id=dto.contractor_id,
        )

        if dto.password is not None:
            # Смена пароля не трогает 2FA (сохраняем текущий флаг пользователя).
            await repo.update_password(user_id_int, hash_password(dto.password))
            # Отзываем живые сессии: скомпрометированная сессия не должна
            # пережить сброс пароля (SEC-14, best-effort).
            await _revoke_sessions(self, user_id_int)

        # Инвалидируем ACM cache при изменении роли (state не меняется через PATCH)
        if dto.role is not None:
            invalidator = _get_invalidator(self)
            if invalidator is not None:
                await invalidator.invalidate_user(user_id_int)

        self.write_json(updated)

    async def delete(self, user_id: str) -> None:
        """Деактивация пользователя"""
        user_id_int = self.get_int_path_arg(user_id, "user_id")

        # updated_by берем из авторизованного пользователя (X-Remote-User)
        if not self.current_user:
            # защита от теоретической гонки, хотя prepare уже вернет 401
            self.write_unauthorized("User authentication required")
            return

        updated_by = self.current_user.id

        repo = UserRepository(self.db)
        await repo.deactivate(user_id_int, updated_by)

        invalidator = _get_invalidator(self)
        if invalidator is not None:
            await invalidator.invalidate_user(user_id_int)

        self.set_status(204)
        self.finish()

class UserReactivateHandler(BaseHandler):
    async def post(self, user_id: str) -> None:
        """Реактивация деактивированного пользователя"""
        user_id_int = self.get_int_path_arg(user_id, "user_id")

        if not self.current_user:
            self.write_unauthorized("User authentication required")
            return

        repo = UserRepository(self.db)
        user = await repo.reactivate(user_id_int, self.current_user.id)

        if not user:
            self.write_not_found(f"User with ID {user_id_int} not found")
            return

        invalidator = _get_invalidator(self)
        if invalidator is not None:
            await invalidator.invalidate_user(user_id_int)

        self.write_json(user)


class UserEnable2FAHandler(BaseHandler):
    async def post(self, user_id: str) -> None:
        """Включить 2FA. Если Max не привязан — вернуть свежую invite-ссылку."""
        user_id_int = self.get_int_path_arg(user_id, "user_id")

        if not self.current_user:
            self.write_unauthorized("User authentication required")
            return

        repo = UserRepository(self.db)
        target = await repo.get_by_id(user_id_int)
        if not target:
            self.write_not_found(f"User with ID {user_id_int} not found")
            return

        # Защита от эскалации: нельзя менять 2FA target'а >= своей роли
        # (SEC-03). Administrator может управлять кем угодно.
        if not can_modify_target(caller_role(self), target.role.value):
            logger.warning(
                "Privilege guard: user_id=%s denied enable-2fa on target=%s "
                "(role=%s)",
                self.current_user.id, user_id_int, target.role.value,
            )
            self.write_forbidden("Insufficient privileges to modify this user")
            return

        # Состояние пользователя здесь не ограничиваем сознательно: 2FA можно
        # настроить на любом существующем аккаунте, как и reactivate.
        auth_record = await repo.get_auth_record(user_id_int)
        if auth_record is None:
            self.write_conflict(
                "User has no password set; set a password before enabling 2FA"
            )
            return

        await repo.set_require_2fa(user_id_int, True)
        # Смена настройки 2FA отзывает живые сессии (SEC-14, best-effort).
        await _revoke_sessions(self, user_id_int)

        if target.max_user_id is not None:
            self.set_status(204)
            self.finish()
            return

        # Max не привязан — выдаём invite-ссылку, чтобы оператор отдал её юзеру.
        bot_username = self.settings.get("max_bot_username", "")
        token_repo = MaxInviteTokenRepository(self.db)
        invite = await token_repo.create_for_user(user_id_int)
        max_invite_link = (
            f"https://max.ru/{bot_username}?start={_urlquote(invite.token, safe='')}"
            if bot_username else None
        )
        self.write_json({
            "user_id": user_id_int,
            "require_2fa": True,
            "max_bound": False,
            "max_invite_token": invite.token,
            "max_invite_link": max_invite_link,
            "max_invite_expires_at": invite.expires_at.isoformat(),
        })


class UserDisable2FAHandler(BaseHandler):
    async def post(self, user_id: str) -> None:
        """Выключить 2FA. Идемпотентно: нет user_auth → нечего выключать."""
        user_id_int = self.get_int_path_arg(user_id, "user_id")

        if not self.current_user:
            self.write_unauthorized("User authentication required")
            return

        repo = UserRepository(self.db)
        target = await repo.get_by_id(user_id_int)
        if not target:
            self.write_not_found(f"User with ID {user_id_int} not found")
            return

        # Защита от эскалации: нельзя снимать 2FA с target'а >= своей роли
        # (SEC-03). Administrator может управлять кем угодно.
        if not can_modify_target(caller_role(self), target.role.value):
            logger.warning(
                "Privilege guard: user_id=%s denied disable-2fa on target=%s "
                "(role=%s)",
                self.current_user.id, user_id_int, target.role.value,
            )
            self.write_forbidden("Insufficient privileges to modify this user")
            return

        if await repo.get_auth_record(user_id_int) is not None:
            await repo.set_require_2fa(user_id_int, False)
            # Смена настройки 2FA отзывает живые сессии (SEC-14, best-effort).
            await _revoke_sessions(self, user_id_int)

        self.set_status(204)
        self.finish()


class UserListHandler(BaseHandler):
    """Обработчик для списка пользователей (GET /users, POST /users)"""

    async def get(self) -> None:
        """Получение списка пользователей с фильтрацией и пагинацией"""
        # Получаем query параметры
        state_raw = self.get_query_argument("state", default=None)
        state: str | None = None
        if state_raw is not None:
            try:
                state = UserState(state_raw).value
            except ValueError:
                self.write_validation_error(f"Invalid state: {state_raw}")
                return

        contractor_id_str = self.get_query_argument("contractor_id", default=None)
        role_raw = self.get_query_argument("role", default=None)
        q_raw = self.get_query_argument("q", default=None)
        try:
            q = normalize_query(q_raw, max_length=_Q_MAX_LENGTH)
        except ValueError as exc:
            self.write_validation_error(str(exc))
            return
        limit_str = self.get_query_argument("limit", default="100") or "100"
        offset_str = self.get_query_argument("offset", default="0") or "0"

        contractor_id = None
        if contractor_id_str:
            try:
                contractor_id = int(contractor_id_str)
            except ValueError:
                self.write_validation_error("contractor_id must be an integer")
                return

        role: UserRole | None = None
        if role_raw is not None:
            try:
                role = UserRole(role_raw)
            except ValueError:
                self.write_validation_error(f"Invalid role: {role_raw}")
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

        repo = UserRepository(self.db)
        users = await repo.get_all(state=state, contractor_id=contractor_id, role=role, q=q, limit=limit, offset=offset)
        total = await repo.count(state=state, contractor_id=contractor_id, role=role, q=q)
        self.write_json(PaginatedResponse(items=users, total=total, limit=limit, offset=offset))

    async def post(self) -> None:
        """Создание нового пользователя"""
        if not self.current_user:
            # защита от теоретической гонки, хотя prepare уже вернет 401
            self.write_unauthorized("User authentication required")
            return

        # Получение и конвертация JSON → DTO
        # dacite автоматически конвертирует строки в enum и валидирует значения
        dto = self.get_json_body_as(CreateUserDTO, config=_DTO_CONFIG)

        # Защита от эскалации: нельзя создать пользователя с ролью выше своей
        # (SEC-01). Иначе operator создал бы administrator-аккаунт и вошёл им.
        caller = caller_role(self)
        if not can_assign_role(caller, dto.role.value):
            logger.warning(
                "Privilege guard: user_id=%s role=%r cannot create user with "
                "role=%s",
                self.current_user.id, caller, dto.role.value,
            )
            self.write_forbidden("Cannot create a user with a role higher than your own")
            return

        created_by = self.current_user.id
        repo = UserRepository(self.db)

        # Проверка на существование пользователя с таким телефоном
        existing = await repo.get_by_phone(dto.phone)
        if existing:
            self.write_conflict(f"User with phone {dto.phone} already exists")
            return

        # Создание пользователя
        # dto.role уже является enum UserRole (не строкой) благодаря dacite cast.
        # get_by_phone выше — TOCTOU-проверка; при гонке двух запросов с одним
        # телефоном INSERT поднимет unique-violation (23505) на users.phone:
        # это конфликт (409), а не серверная ошибка (500).
        try:
            user = await repo.create(
                phone=dto.phone,
                full_name=dto.full_name,
                created_by=created_by,
                contractor_id=dto.contractor_id,
                role=dto.role
            )
        except Exception as exc:
            if is_unique_violation(exc):
                logger.info("Duplicate phone on user create (race): hash=%s", _redact_phone(dto.phone))
                self.write_conflict(f"User with phone {dto.phone} already exists")
                return
            raise

        if dto.password is not None:
            # Новые пользователи получают 2FA по умолчанию.
            await repo.set_password_hash(user.id, hash_password(dto.password), require_2fa=True)
            # `user` собран из get_by_id ДО создания строки user_auth, поэтому
            # его require_2fa=False. Синхронизируем ответ на создание с реальным
            # состоянием (2FA включён по умолчанию), чтобы create-ответ совпадал
            # с последующим GET и примером в OpenAPI.
            user.require_2fa = True

        # Выпускаем invite-токен для привязки Max-бота. Админ передаёт
        # ссылку пользователю; пользователь жмёт «Начать» в Max,
        # notification_api-поллер ловит bot_started, идёт в
        # /internal/user/by-invite и проставляет users.max_user_id.
        bot_username = self.settings.get("max_bot_username", "")
        token_repo = MaxInviteTokenRepository(self.db)
        invite = await token_repo.create_for_user(user.id)
        max_invite_link = (
            f"https://max.ru/{bot_username}?start={_urlquote(invite.token, safe='')}"
            if bot_username else None
        )

        payload = asdict(user)
        payload["max_invite_token"] = invite.token
        payload["max_invite_expires_at"] = invite.expires_at.isoformat()
        payload["max_invite_link"] = max_invite_link
        self.write_json(payload, status_code=201)
