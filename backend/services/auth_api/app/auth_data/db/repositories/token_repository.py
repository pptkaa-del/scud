from __future__ import annotations

from datetime import datetime

from shared.database import AsyncPostgres

from ..models.tokens import AccessToken


class TokenRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def create(
        self,
        user_id: int,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AccessToken:
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO access_tokens (user_id, token_hash, expires_at, user_agent, ip_address)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, token_hash, expires_at, user_agent, ip_address),
        )
        return AccessToken(**row)

    async def get_by_hash(self, token_hash: str) -> AccessToken | None:
        row = await self._db.fetch_one(
            """
            SELECT * FROM access_tokens
            WHERE token_hash = %s
              AND revoked_at IS NULL
              AND expires_at > now()
            """,
            (token_hash,),
        )
        if not row:
            return None
        return AccessToken(**row)

    async def revoke(self, token_hash: str) -> bool:
        row = await self._db.fetch_one(
            """
            UPDATE access_tokens
            SET revoked_at = now()
            WHERE token_hash = %s AND revoked_at IS NULL
            RETURNING id
            """,
            (token_hash,),
            read_only=False,
        )
        return row is not None

    async def rotate(
        self,
        old_token_hash: str,
        new_token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AccessToken | None:
        """Атомарная ротация: отзыв старого + создание нового в одном SQL.

        Returns:
            Новый AccessToken при успехе, None если old_token_hash уже отозван
            или не найден (caller должен ответить 401).
        """
        row = await self._db.fetch_one(
            """
            WITH revoked AS (
                UPDATE access_tokens
                SET revoked_at = now()
                WHERE token_hash = %s AND revoked_at IS NULL
                RETURNING user_id
            )
            INSERT INTO access_tokens (user_id, token_hash, expires_at, user_agent, ip_address)
            SELECT user_id, %s, %s, %s, %s FROM revoked
            RETURNING *
            """,
            (old_token_hash, new_token_hash, expires_at, user_agent, ip_address),
            read_only=False,
        )
        if not row:
            return None
        return AccessToken(**row)

    async def revoke_by_user_id(self, user_id: int) -> int:
        """Отзывает все активные токены пользователя. Возвращает количество отозванных.

        Один атомарный ``UPDATE ... RETURNING`` (COR-30): счётчик берётся из
        числа реально обновлённых строк, а не из отдельного ``SELECT COUNT``.
        При гонке двух параллельных revoke каждый токен отзывается ровно один
        раз, и сумма возвращённых count по конкурентным вызовам равна числу
        активных токенов — без двойного учёта прежней read-then-write схемы.
        """
        rows = await self._db.fetch_all(
            """
            UPDATE access_tokens
            SET revoked_at = now()
            WHERE user_id = %s AND revoked_at IS NULL
            RETURNING id
            """,
            (user_id,),
            read_only=False,
        )
        return len(rows)

    async def delete_expired(self) -> None:
        await self._db.execute(
            "DELETE FROM access_tokens WHERE expires_at < now()",
        )
