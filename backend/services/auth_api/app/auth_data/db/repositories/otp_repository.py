from __future__ import annotations

from datetime import datetime
from typing import Any

from shared.database import AsyncPostgres

from ..models.tokens import OtpCode


class OtpRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def create(self, phone: str, code_hash: str, expires_at: datetime) -> OtpCode:
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO otp_codes (phone, code_hash, expires_at)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (phone, code_hash, expires_at),
        )
        return OtpCode(**row)

    async def get_active(self, phone: str) -> OtpCode | None:
        """Вернуть последний неиспользованный, не просроченный OTP для телефона."""
        row = await self._db.fetch_one(
            """
            SELECT * FROM otp_codes
            WHERE phone = %s
              AND used_at IS NULL
              AND expires_at > now()
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (phone,),
        )
        if not row:
            return None
        return OtpCode(**row)

    async def consume(self, otp_id: int) -> bool:
        """Атомарно «сжечь» OTP как single-use (SEC-12).

        Один CAS-``UPDATE`` помечает ``used_at`` ровно для ещё не
        использованного и не просроченного кода. При двух параллельных
        ``verify`` ровно один получит строку в ``RETURNING``; второй увидит
        ``used_at IS NOT NULL`` и вернёт 0 строк.

        Returns:
            True — OTP успешно потреблён (вызывающий вправе выдать токен).
            False — OTP уже использован или истёк; вызывающий ОБЯЗАН
            отклонить запрос.
        """
        row = await self._db.fetch_one(
            """
            UPDATE otp_codes SET used_at = now()
            WHERE id = %s AND used_at IS NULL AND expires_at > now()
            RETURNING id
            """,
            (otp_id,),
            read_only=False,
        )
        return row is not None

    async def reserve_attempt(
        self, otp_id: int, max_attempts: int,
    ) -> dict[str, Any] | None:
        """Атомарно зарезервировать попытку перед сравнением кода (SEC-13).

        Один CAS-``UPDATE`` инкрементирует ``attempts`` ровно тогда, когда OTP
        ещё активен и бюджет не исчерпан. При N параллельных попытках каждая
        расходует бюджет ровно один раз — гонка check→compare→increment больше
        не обходит лимит ``max_attempts``. ``max_attempts`` приходит из
        конфигурации сервиса (``otp_max_attempts``), в таблице его нет.

        Returns:
            ``{"attempts", "code_hash"}`` при успешном резерве; ``None`` —
            бюджет исчерпан, OTP использован или истёк.
        """
        row = await self._db.fetch_one(
            """
            UPDATE otp_codes SET attempts = attempts + 1
            WHERE id = %s
              AND attempts < %s
              AND used_at IS NULL
              AND expires_at > now()
            RETURNING attempts, code_hash
            """,
            (otp_id, max_attempts),
            read_only=False,
        )
        if row is None:
            return None
        return dict(row)

    async def delete_expired(self) -> None:
        await self._db.execute(
            "DELETE FROM otp_codes WHERE expires_at < now()",
        )
