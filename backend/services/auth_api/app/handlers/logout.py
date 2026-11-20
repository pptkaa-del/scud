"""
POST /api/v1/auth/logout

Отзыв сессионного cookie.

Cookie: access_token=<opaque>  (опционально — idempotent если нет)
Response 204: No Content
"""
import logging

from app.auth_data.db.repositories.token_repository import TokenRepository
from shared.utils import hash_token

from .base import BaseHandler

logger = logging.getLogger(__name__)


class LogoutHandler(BaseHandler):
    """
    Логаут: отзывает access token из БД и очищает cookie.
    Не возвращает ошибку если токен уже недействителен (idempotent).
    """

    async def post(self) -> None:
        raw_cookie = self.get_cookie("access_token")
        if raw_cookie:
            token_repo = TokenRepository(self.db)
            await token_repo.revoke(hash_token(raw_cookie))

        self.set_header(
            "Set-Cookie",
            "access_token=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
        )

        self.set_status(204)
        self.finish()
