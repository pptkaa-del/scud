"""
GET /acm/identity — резолвит session-cookie в роль для выбора ролевой папки
фронтенда gateway'ем. БЕЗ авторизации по пути (это остаётся в /acm/check).

Internal-only; наружу не проксируется (gateway ходит напрямую). Переиспользует
валидацию токена и загрузку UserContext из BaseHandler.

Ответы:
  200 — заголовок X-Remote-Role: worker|operator|administrator (без тела)
  401 — токен отсутствует/невалиден/истёк, либо пользователь деактивирован
  503 — auth_api недоступен (fail closed)
"""
import logging

from shared.enums import UserState
from shared.http import UpstreamUnavailableError

from ..cache import UserContextCache
from .base import BaseHandler

logger = logging.getLogger(__name__)


class IdentityHandler(BaseHandler):
    """Резолвит валидный access_token в роль. Без проверки прав по пути."""

    async def get(self) -> None:
        token = self.request.headers.get("X-Access-Token", "").strip()
        if not token:
            self.write_unauthorized("Missing authentication token")
            return

        try:
            user_id = await self._validate_token(token)
        except UpstreamUnavailableError as exc:
            self.write_service_unavailable(f"auth_api unreachable: {exc}")
            return
        if user_id is None:
            self.write_unauthorized("Invalid or expired session")
            return

        cache: UserContextCache = self.settings["cache"]
        ctx = await cache.get_or_load(user_id, self._load_user_context)
        if ctx is None:
            self.write_unauthorized("User not found")
            return

        if ctx.state != UserState.ACTIVE:
            await cache.invalidate(user_id)
            self.write_unauthorized("User account is not active")
            return

        self.set_header("X-User-Id", str(user_id))
        self.set_header("X-Remote-Role", ctx.role)
        self.set_status(200)
        self.finish()
