"""Internal session-revoke endpoint.

``POST /internal/api/v1/sessions/revoke`` — отзывает все активные access
tokens пользователя. Вызывается user_api после успешной смены учётных
данных (пароль / переключение 2FA), чтобы скомпрометированная живая сессия
не пережила сброс пароля (SEC-14).

Запрос подписывается ``X-Internal-Timestamp`` + ``X-Internal-Signature``
ключом ``INTERNAL_SESSION_REVOKE_HMAC_KEY`` поверх
``timestamp + METHOD + path + sha256(body)`` (тот же generic-формат, что у
прочих inter-service каналов, подпись привязана к method+path — SEC-20).

Семантика ключа: ключ задан в settings → подпись обязательна (401 без тела
до любого обращения к БД); ключ не задан → fail-open для dev/CI. В
production main.py обязан установить ключ (``required=is_production()``).
"""
from __future__ import annotations

import json
import logging

from app.auth_data.db.repositories.token_repository import TokenRepository
from shared.utils import body_sha256, verify_request_signature

from .base import BaseHandler

logger = logging.getLogger(__name__)

# Имена HTTP-заголовков inter-service HMAC-подписи (общие для всех каналов).
_HDR_TIMESTAMP = "X-Internal-Timestamp"
_HDR_SIGNATURE = "X-Internal-Signature"

# Ключ settings, под которым main.py кладёт bytes-ключ HMAC этого канала.
_SETTING_SESSION_REVOKE_KEY = "internal_session_revoke_hmac_key"

# Подписываемые (method, path) — должны побайтово совпасть с тем, что
# отправитель (user_api AuthApiSessionClient) подписывает и POST'ит.
SESSION_REVOKE_METHOD = "POST"
SESSION_REVOKE_PATH = "/internal/api/v1/sessions/revoke"


class SessionRevokeHandler(BaseHandler):
    """POST /internal/api/v1/sessions/revoke.

    Body: ``{"user_id": <int>}``.
    Response 200: ``{"revoked": <N>}`` — число отозванных токенов.
    Response 400: тело невалидно (не JSON-объект / user_id не int).
    Response 401: подпись требуется, но невалидна/отсутствует.
    """

    def _verify_signature(self) -> bool:
        hmac_key = self.settings.get(_SETTING_SESSION_REVOKE_KEY)
        if not hmac_key:
            # Ключ не настроен — режим обратной совместимости (dev/CI).
            return True
        timestamp = self.request.headers.get(_HDR_TIMESTAMP, "")
        signature = self.request.headers.get(_HDR_SIGNATURE, "")
        # Подписывается raw-body (sha256), не пересобранный dict — хэш должен
        # совпасть байт-в-байт с тем, что подписал отправитель.
        return verify_request_signature(
            signature=signature,
            timestamp=timestamp,
            method=self.request.method or "",
            path=self.request.path,
            body_sha256_hex=body_sha256(self.request.body or b""),
            key=hmac_key,
        )

    async def post(self) -> None:
        if not self._verify_signature():
            # Не логируем тело/подпись — это могут быть секреты. Логируем факт
            # отказа с IP, чтобы заметить попытку обхода.
            logger.warning(
                "sessions/revoke signature rejected from %s",
                self.request.remote_ip,
            )
            self.write_unauthorized("Invalid or expired signature")
            return

        try:
            body = json.loads(self.request.body or b"{}")
        except (ValueError, TypeError):
            self.write_validation_error("Invalid JSON body")
            return
        if not isinstance(body, dict):
            self.write_validation_error("Body must be a JSON object")
            return

        raw_user_id = body.get("user_id")
        # bool — подкласс int; явно отвергаем, чтобы True не стал user_id=1.
        if isinstance(raw_user_id, bool) or not isinstance(raw_user_id, int):
            self.write_validation_error("user_id must be an integer")
            return

        repo = TokenRepository(self.db)
        revoked = await repo.revoke_by_user_id(raw_user_id)
        logger.info(
            "Revoked %d session token(s) for user_id=%d after credential change",
            revoked, raw_user_id,
        )
        self.write_json({"revoked": revoked})
