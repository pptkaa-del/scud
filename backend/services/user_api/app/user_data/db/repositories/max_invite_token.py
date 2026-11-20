"""Репозиторий одноразовых invite-токенов для привязки Max-бота к пользователю."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import secrets

from shared.database import AsyncPostgres
from shared.database.errors import QueryError

_DEFAULT_TTL_SECONDS = 24 * 3600  # сутки


@dataclass(slots=True)
class MaxInviteToken:
    token: str
    user_id: int
    expires_at: datetime
    used_at: datetime | None
    created_at: datetime


class MaxInviteTokenRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def create_for_user(
        self, user_id: int, ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> MaxInviteToken:
        """Сгенерировать новый токен с TTL и сохранить.

        Каждый вызов выдаёт свежий случайный токен — старые активные
        токены того же user'а остаются валидными до собственного истечения;
        первый успешный `bot_started` погасит свой и проставит max_user_id.
        """
        token = secrets.token_urlsafe(32)
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO max_invite_tokens (token, user_id, expires_at)
            VALUES (%s, %s, now() + (%s || ' seconds')::interval)
            RETURNING token, user_id, expires_at, used_at, created_at
            """,
            (token, user_id, ttl_seconds),
        )
        return MaxInviteToken(**row)

    async def consume_active(self, token: str) -> MaxInviteToken | None:
        """Атомарно пометить токен used_at=now() при условии что он ещё активен.

        Возвращает MaxInviteToken (с user_id) если консьюмить удалось,
        None — если токен не найден, истёк или уже использован.

        В одной транзакции защищает от двойного срабатывания (если бот
        прислал bot_started дважды для одного start_payload).
        """
        try:
            row = await self._db.fetch_one(
                """
                UPDATE max_invite_tokens
                SET used_at = now()
                WHERE token = %s
                  AND used_at IS NULL
                  AND expires_at > now()
                RETURNING token, user_id, expires_at, used_at, created_at
                """,
                (token,),
                read_only=False,
            )
        except QueryError:
            return None
        if row is None:
            return None
        return MaxInviteToken(**row)
