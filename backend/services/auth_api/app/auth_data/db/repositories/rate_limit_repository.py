from __future__ import annotations

from shared.database import AsyncPostgres


class RateLimitRepository:
    """Атомарный rate-limit через PostgreSQL upsert.

    Окно скользящее по window_start: если NOW() - window_start превысило
    window_seconds, окно перезапускается (attempts=1, window_start=NOW()).
    Иначе attempts увеличивается.
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def check_and_record(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> bool:
        """Атомарно увеличить счётчик и вернуть True если в пределах limit.

        Returns:
            True если запрос разрешён (текущая попытка <= limit).
            False если rate-limit превышен.
        """
        sql = """
        INSERT INTO login_rate_limit (key, attempts, window_start)
        VALUES (%s, 1, NOW())
        ON CONFLICT (key) DO UPDATE SET
            attempts = CASE
                WHEN NOW() - login_rate_limit.window_start > make_interval(secs => %s)
                THEN 1
                ELSE login_rate_limit.attempts + 1
            END,
            window_start = CASE
                WHEN NOW() - login_rate_limit.window_start > make_interval(secs => %s)
                THEN NOW()
                ELSE login_rate_limit.window_start
            END
        RETURNING attempts;
        """
        # INSERT … ON CONFLICT … DO UPDATE RETURNING всегда возвращает строку
        # (или INSERTed, или UPDATEd). Strict-вариант ловит регрессию, если
        # кто-то поменяет CTE на DO NOTHING.
        row = await self._db.fetch_one_strict(
            sql,
            (key, window_seconds, window_seconds),
        )
        return int(row["attempts"]) <= limit

    async def delete_stale(self, retention_seconds: int) -> None:
        """Удалить строки, чьё окно старше retention_seconds (COR-28).

        Таблица login_rate_limit иначе растёт неограниченно: на каждый новый
        IP создаётся строка, которая после истечения окна больше не нужна.
        Удаление по ``window_start`` безопасно — устаревшая строка всё равно
        перезапустила бы окно при следующем check_and_record.
        """
        await self._db.execute(
            """
            DELETE FROM login_rate_limit
            WHERE window_start < now() - make_interval(secs => %s)
            """,
            (retention_seconds,),
        )
