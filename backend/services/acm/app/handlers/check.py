"""
GET /acm/check — основной эндпоинт ACM.

При сбое auth_api (5xx/network) handler отвечает 503, а не 401: иначе
upstream-падение auth_api маскировалось бы как «invalid session» в логах
операторы и мониторинг не смогли бы отличить реальную атаку (массовые 401)
от падения сервиса аутентификации.

Вызывается Apache Lua перед каждым проксируемым запросом.
Проверяет токен, права и возвращает контекстные заголовки.

Входные заголовки:
  X-Access-Token   — токен сессии из cookie
  X-Request-Path   — путь исходного запроса
  X-Request-Method — HTTP-метод исходного запроса

Ответные заголовки при 200:
  X-User-Id              — идентификатор пользователя
  X-Remote-Role          — роль пользователя
  X-Remote-Lockers       — UUID шкафов с Допуском через запятую или "*"
  X-Remote-Require-2FA   — "true"/"false"; нужна ли 2FA для critical-операций

Коды ответа:
  200 — доступ разрешён, заголовки выставлены
  401 — токен отсутствует, истёк или недействителен
  403 — доступ запрещён (недостаточно прав или нет Допуска)
"""
import logging

from shared.enums import UserState
from shared.http import UpstreamUnavailableError

from ..cache import UserContextCache
from ..rules import Role, check_access, find_rule
from .base import BaseHandler

logger = logging.getLogger(__name__)


class CheckHandler(BaseHandler):
    """
    Проверяет права доступа для входящего запроса.

    Алгоритм:
    1. Для публичных путей — немедленно возвращает 200 без проверки токена.
    2. Извлекает X-Access-Token → валидирует через auth_api → получает user_id.
    3. Загружает UserContext из кэша (Redis) или БД.
    4. Проверяет state пользователя из ctx.state (deactivated → 403).
       Реактивная инвалидация через user_api при изменении state/role.
    5. Проверяет матрицу разрешений.
    6. Для worker с locker-scoped путём проверяет наличие Допуска.
    7. Выставляет заголовки X-User-Id, X-Remote-Role, X-Remote-Lockers,
       X-Remote-Require-2FA.
    """

    async def get(self) -> None:
        method = self.request.headers.get("X-Request-Method", "GET").strip()
        path = self.request.headers.get("X-Request-Path", "").strip()

        if not path:
            self.write_forbidden("X-Request-Path header is required")
            return

        # 1. Публичный эндпоинт — пропускаем без токена
        rule = find_rule(method, path)
        if rule is not None and rule.min_role is None:
            self.set_status(200)
            self.finish()
            return

        # 2. Извлечение и валидация токена
        token = self.request.headers.get("X-Access-Token", "").strip()
        if not token:
            self.write_unauthorized("Missing authentication token")
            return

        try:
            user_id = await self._validate_token(token)
        except UpstreamUnavailableError as exc:
            # 503 вместо 401: upstream auth_api лежит, не маскируем как
            # invalid session (иначе мониторинг/операторы не отличат от атаки).
            self.write_service_unavailable(f"auth_api unreachable: {exc}")
            return
        if user_id is None:
            self.write_unauthorized("Invalid or expired session")
            return

        # 3. Загрузка UserContext (кэш Redis → БД) с singleflight,
        # чтобы при cold-cache stampede только одна корутина шла в БД.
        cache: UserContextCache = self.settings["cache"]
        ctx = await cache.get_or_load(user_id, self._load_user_context)
        if ctx is None:
            self.write_unauthorized("User not found")
            return

        # 4. Проверка состояния пользователя по кэшированному ctx.state
        if ctx.state != UserState.ACTIVE:
            await cache.invalidate(user_id)
            self.write_forbidden("User account is not active")
            return

        # 5 & 6. Проверка матрицы разрешений + Допуска для шкафов
        try:
            role = Role.from_str(ctx.role)
        except ValueError:
            logger.error("Unknown role %r for user_id=%d", ctx.role, user_id)
            self.write_forbidden("Unknown user role")
            return

        allowed, reason = check_access(
            method=method,
            path=path,
            role=role,
            locker_ids=list(ctx.locker_ids),
            full_access=ctx.full_access,
        )

        if not allowed:
            logger.info(
                "Access denied: user_id=%d role=%s method=%s path=%s reason=%s",
                user_id, ctx.role, method, path, reason,
            )
            self.write_forbidden(reason)
            return

        # 7. Выставляем контекстные заголовки
        self.set_header("X-User-Id", str(user_id))
        self.set_header("X-Remote-Role", ctx.role)
        if ctx.full_access or role >= Role.OPERATOR:
            self.set_header("X-Remote-Lockers", "*")
        else:
            self.set_header("X-Remote-Lockers", ",".join(ctx.locker_ids))
        self.set_header("X-Remote-Require-2FA", "true" if ctx.require_2fa else "false")

        self.set_status(200)
        self.finish()
