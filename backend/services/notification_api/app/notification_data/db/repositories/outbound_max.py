"""Репозиторий очереди исходящих сообщений в Max-бот (`outbound_max_messages`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.database import AsyncPostgres
from shared.enums import NotificationChannel

# Сколько держать терминальные (sent/failed) записи после финализации,
# прежде чем purge их удалит. body у них уже обнулён, остаётся только
# метаинформация (attempts/last_error) для краткосрочной диагностики.
TERMINAL_RETENTION_SECONDS = 86400  # 24 часа


@dataclass
class OutboundMaxMessage:
    """Запись в outbound_max_messages.

    `max_user_id IS NULL` — пользователь ещё не привязал Max-бот, запись
    ждёт `bot_started`-события. После привязки long-polling воркер
    обновит max_user_id и попытается доставить.
    """

    id: int
    user_id: int
    max_user_id: int | None
    channel: str
    address: str | None
    body: str
    purpose: str
    attempts: int
    sent_at: datetime | None
    failed_at: datetime | None
    last_error: str | None
    created_at: datetime
    expires_at: datetime


class OutboundMaxRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def insert(
        self,
        *,
        user_id: int,
        body: str,
        purpose: str,
        ttl_seconds: int,
        channel: str = NotificationChannel.MAX.value,
        address: str | None = None,
        max_user_id: int | None = None,
    ) -> int:
        """Поставить сообщение в очередь. Возвращает id новой записи."""
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO outbound_max_messages
                (user_id, max_user_id, body, purpose, channel, address, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, now() + (%s || ' seconds')::interval)
            RETURNING id
            """,
            (user_id, max_user_id, body, purpose, channel, address, ttl_seconds),
        )
        return int(row["id"])

    async def mark_sent(self, message_id: int) -> None:
        # body = NULL: после доставки plaintext OTP больше не нужен и не
        # должен оседать в БД/бэкапах (SEC-18).
        await self._db.execute(
            """
            UPDATE outbound_max_messages
            SET sent_at = now(), attempts = attempts + 1, body = NULL
            WHERE id = %s
            """,
            (message_id,),
        )

    async def mark_failed(self, message_id: int, error: str) -> None:
        # body = NULL: терминальный сбой — код уже не будет доставлен,
        # хранить plaintext OTP незачем (SEC-18).
        await self._db.execute(
            """
            UPDATE outbound_max_messages
            SET failed_at = now(), attempts = attempts + 1, last_error = %s,
                body = NULL
            WHERE id = %s
            """,
            (error, message_id),
        )

    async def increment_attempt(self, message_id: int, error: str) -> None:
        """Учесть неуспешную попытку без финализации (повтор позднее)."""
        await self._db.execute(
            """
            UPDATE outbound_max_messages
            SET attempts = attempts + 1, last_error = %s
            WHERE id = %s
            """,
            (error, message_id),
        )

    async def select_pending_for_user(self, user_id: int) -> list[OutboundMaxMessage]:
        """Активные (не sent / не failed / не expired) записи пользователя.

        Используется поллером после получения bot_started, чтобы
        доставить накопленные OTP, пока пользователь привязывал бот.
        """
        rows = await self._db.fetch_all(
            """
            SELECT id, user_id, max_user_id, channel, address, body, purpose, attempts,
                   sent_at, failed_at, last_error, created_at, expires_at
            FROM outbound_max_messages
            WHERE user_id = %s
              AND sent_at IS NULL
              AND failed_at IS NULL
              AND expires_at > now()
            ORDER BY created_at
            """,
            (user_id,),
        )
        return [OutboundMaxMessage(**row) for row in rows]

    async def purge_expired(self) -> None:
        """Удалить просроченные и старые терминальные записи (SEC-18).

        - `expires_at < now()` — OTP уже бесполезен после истечения TTL, даже
          если он ещё pending (живые pending с `expires_at > now()` не
          трогаем — их код ещё валиден и ждёт доставки после bot_started).
        - терминальные (sent/failed) старше окна ретенции — их body уже
          обнулён, остаётся только мета-инфо для краткосрочной диагностики.

        Идемпотентно; вызывается периодически из планировщика.
        """
        await self._db.execute(
            """
            DELETE FROM outbound_max_messages
            WHERE expires_at < now()
               OR (
                   (sent_at IS NOT NULL OR failed_at IS NOT NULL)
                   AND created_at < now() - (%s || ' seconds')::interval
               )
            """,
            (TERMINAL_RETENTION_SECONDS,),
        )

    async def set_max_user_id_for_pending(
        self, user_id: int, max_user_id: int,
    ) -> None:
        """Проставить max_user_id всем pending-записям пользователя."""
        await self._db.execute(
            """
            UPDATE outbound_max_messages
            SET max_user_id = %s
            WHERE user_id = %s
              AND max_user_id IS NULL
              AND sent_at IS NULL
              AND failed_at IS NULL
            """,
            (max_user_id, user_id),
        )
