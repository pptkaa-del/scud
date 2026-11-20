"""
POST /api/v1/auth/access

Ротация сессионного cookie (access token).

Cookie: access_token=<opaque>
Response 200: {"message": "Session renewed"} + Set-Cookie: access_token=<new>; Path=/
Response 401: если access token невалиден или просрочен
"""
from datetime import UTC, datetime, timedelta
import logging
import secrets

from app.auth_data.db.repositories.token_repository import TokenRepository
from shared.utils import hash_token

from .base import BaseHandler

logger = logging.getLogger(__name__)


class AccessHandler(BaseHandler):
    """Ротирует сессионный access token cookie."""

    async def post(self) -> None:
        raw_cookie = self.get_cookie("access_token")
        if not raw_cookie:
            self.write_unauthorized("Missing access_token cookie")
            return

        token_hash = hash_token(raw_cookie)
        token_repo = TokenRepository(self.db)
        stored = await token_repo.get_by_hash(token_hash)

        if not stored:
            self.write_unauthorized("Invalid or expired access token")
            return

        new_raw = secrets.token_urlsafe(32)
        new_hash = hash_token(new_raw)
        expires_at = datetime.now(UTC) + timedelta(hours=self.access_token_ttl_hours())

        new_token = await token_repo.rotate(
            old_token_hash=token_hash,
            new_token_hash=new_hash,
            expires_at=expires_at,
            user_agent=self.request.headers.get("User-Agent"),
            ip_address=self.request.remote_ip,
        )
        if new_token is None:
            self.write_unauthorized("Token already rotated or revoked")
            return

        self._set_access_cookie(new_raw)
        self.write_json({"message": "Session renewed"})
