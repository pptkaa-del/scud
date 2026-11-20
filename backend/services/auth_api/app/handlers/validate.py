"""
Internal endpoint для проверки сессии (access token).

Вызывается только Apache Lua через curl.
Снаружи недоступен (закрыт в apache.conf через Require all denied).

GET /auth/validate
Headers: X-Access-Token: <opaque_token>
Response 200: X-User-Id: <user_id>
Response 401: (no body)
"""
import logging

from app.auth_data.db.repositories.token_repository import TokenRepository
from shared.auth import JSONHandler
from shared.utils import hash_token

logger = logging.getLogger(__name__)


class ValidateHandler(JSONHandler):
    """
    Проверяет access token как сессионный идентификатор.

    Проверки:
    1. Наличие X-Access-Token header
    2. Токен существует в БД и не истёк/не отозван

    При успехе устанавливает заголовок X-User-Id для Apache Lua.
    """

    async def get(self) -> None:
        token = self.request.headers.get("X-Access-Token", "").strip()
        if not token:
            self.set_status(401)
            self.finish()
            return

        token_repo = TokenRepository(self.db)
        stored = await token_repo.get_by_hash(hash_token(token))

        if not stored:
            logger.debug("Access token not found or expired")
            self.set_status(401)
            self.finish()
            return

        self.set_header("X-User-Id", str(stored.user_id))
        self.set_status(200)
        self.finish()
