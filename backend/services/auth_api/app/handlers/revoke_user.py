"""
DELETE /api/v1/auth/users/{user_id}/tokens

Отзывает все активные сессии (access tokens) указанного пользователя.
Только для администраторов.

Headers: X-Remote-User: <caller_id>  (устанавливается Apache Lua)
Response 200: {"revoked": <N>}  — количество отозванных токенов
Response 400: невалидный user_id
Response 401: нет сессии (X-Remote-User отсутствует)
Response 403: недостаточно прав (не администратор)
"""
import logging

from app.auth_data.db.repositories.token_repository import TokenRepository
from app.services.user_api_client import UserApiClient
from shared.enums import UserRole
from shared.http import UpstreamUnavailableError

from .base import BaseHandler

logger = logging.getLogger(__name__)


class RevokeUserTokensHandler(BaseHandler):
    """
    Отзывает все активные access tokens указанного пользователя.
    Доступен только администраторам (проверяется через User API).
    """

    async def delete(self, user_id: str) -> None:
        # Валидация target user_id из URL
        target_id = self.get_int_path_arg(user_id, "user_id")

        # Получаем ID вызывающего из заголовка (установлен Apache Lua)
        caller_id_raw = self.request.headers.get("X-Remote-User", "").strip()
        if not caller_id_raw:
            self.write_unauthorized("Authentication required")
            return

        try:
            caller_id = int(caller_id_raw)
        except ValueError:
            self.write_unauthorized("Invalid X-Remote-User header")
            return

        # Проверяем роль вызывающего через User API
        try:
            role = await self._get_user_role(caller_id)
        except UpstreamUnavailableError:
            self.write_service_unavailable("Authentication backend is unavailable")
            return
        if role is None:
            self.write_unauthorized("Could not verify caller identity")
            return
        if role != UserRole.ADMINISTRATOR:
            self.write_forbidden("Administrator access required")
            return

        # Отзываем все активные токены целевого пользователя
        token_repo = TokenRepository(self.db)
        revoked = await token_repo.revoke_by_user_id(target_id)

        logger.info(
            "Admin %d revoked %d token(s) for user %d",
            caller_id, revoked, target_id,
        )

        self.write_json({"revoked": revoked})

    async def _get_user_role(self, user_id: int) -> str | None:
        """Возвращает роль пользователя из User API или None если пользователь не найден.

        Raises:
            UpstreamUnavailableError если user_api недоступен.
        """
        client: UserApiClient = self.settings["user_api_client"]
        data = await client.get_user(user_id)
        if data is None:
            return None
        return data.get("role")
