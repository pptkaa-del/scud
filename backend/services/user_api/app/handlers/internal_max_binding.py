"""Internal handler привязки Max-бота к пользователю.

POST /internal/user/by-invite — вызывается notification_api'шным
MaxBotPoller после события `bot_started`. Body содержит invite-token
(который пользователь передал в `start=<token>` deep-link) и числовой
`max_user_id` (тот, кто нажал «Начать»). Handler атомарно проставляет
`users.max_user_id` и помечает токен `used_at=now()`.

Безопасность:
- /internal/* закрыт от внешнего доступа Apache'м.
- Каждый запрос подписан HMAC-SHA256 ключом ``INTERNAL_MAX_BINDING_HMAC_KEY``
  (отдельный от ``INTERNAL_OTP_HMAC_KEY``, чтобы компрометация одного
  канала не давала прав в обоих).

Ответы:
- 204: привязка установлена.
- 401: подпись невалидна / просрочена.
- 400: невалидный DTO.
- 404: токен не найден / истёк / уже использован.
- 409: max_user_id уже привязан к другому активному аккаунту.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

from dacite import Config, from_dict
from dacite.exceptions import MissingValueError, WrongTypeError
from tornado.web import Finish

from app.user_data.db.repositories.max_invite_token import MaxInviteTokenRepository
from app.user_data.db.repositories.user import UserRepository
from shared.auth import ProblemDetailsHandler
from shared.utils import body_sha256, verify_request_signature

logger = logging.getLogger(__name__)

# Верхняя граница длины invite-токена. Реальный токен — secrets.token_urlsafe(32)
# (~43 символа), колонка max_invite_tokens.token — VARCHAR(64). 256 с запасом
# перекрывает легитимный токен и отсекает раздутый payload (SEC-21) ДО
# обращения к БД.
MAX_INVITE_TOKEN_LEN = 256


@dataclass
class BindMaxDTO:
    invite_token: str
    max_user_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.invite_token, str) or not self.invite_token:
            raise ValueError("invite_token must be a non-empty string")
        if len(self.invite_token) > MAX_INVITE_TOKEN_LEN:
            raise ValueError("invite_token is too long")
        if not isinstance(self.max_user_id, int) or self.max_user_id <= 0:
            raise ValueError("max_user_id must be a positive integer")


class InternalMaxBindingHandler(ProblemDetailsHandler):
    """POST /internal/user/by-invite — привязать max_user_id по invite-токену."""

    def _verify_signature(self) -> bool:
        hmac_key = self.settings.get("internal_max_binding_hmac_key")
        if hmac_key is None:
            return True
        timestamp = self.request.headers.get("X-Internal-Timestamp", "")
        signature = self.request.headers.get("X-Internal-Signature", "")
        return verify_request_signature(
            signature=signature,
            timestamp=timestamp,
            method=self.request.method or "",
            path=self.request.path,
            body_sha256_hex=body_sha256(self.request.body or b""),
            key=hmac_key,
        )

    def _parse_dto(self) -> BindMaxDTO:
        try:
            body_dict = self.get_json_body()
            return from_dict(BindMaxDTO, body_dict, config=Config())
        except (KeyError, TypeError, ValueError, MissingValueError, WrongTypeError) as exc:
            logger.warning("Internal max-binding: invalid body: %s", exc)
            self.write_validation_error(str(exc))
            raise Finish from None

    async def post(self) -> None:
        if not self._verify_signature():
            logger.warning(
                "internal/user/by-invite signature rejected from %s",
                self.request.remote_ip,
            )
            self.write_unauthorized("Invalid or expired signature")
            return

        dto = self._parse_dto()

        tokens = MaxInviteTokenRepository(self.db)
        token_row = await tokens.consume_active(dto.invite_token)
        if token_row is None:
            # Не палим конкретику (истёк vs неверный vs использован) —
            # в логах есть. Клиенту наружу — 404.
            logger.info(
                "max-binding: invite token rejected (unknown/expired/used)",
            )
            self.write_not_found("invite_token unknown, expired or already used")
            return

        users = UserRepository(self.db)
        bound = await users.set_max_user_id(token_row.user_id, dto.max_user_id)
        if not bound:
            # set_max_user_id вернул False = users row missing
            # ИЛИ конфликт partial unique. Чаще всего это конфликт —
            # отдаём 409 чтобы воркер логировал "max_user_id уже занят".
            logger.warning(
                "max-binding: failed (user_id=%s max_user_id=%s) — conflict or missing user",
                token_row.user_id, dto.max_user_id,
            )
            self.write_conflict("max_user_id already bound to another user")
            return

        logger.info(
            "Max-binding ok: user_id=%s max_user_id=%s",
            token_row.user_id, dto.max_user_id,
        )
        # Возвращаем user_id — воркеру нужно для последующей доставки
        # pending OTP записей.
        self.write_json({"user_id": token_row.user_id}, status_code=200)
