"""Репозиторий single-row состояния long-polling воркера Max-бота."""

from __future__ import annotations

from shared.database import AsyncPostgres


class BotPollerStateRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_marker(self) -> int | None:
        row = await self._db.fetch_one("SELECT marker FROM bot_poller_state WHERE id = 1")
        if row is None:
            return None
        marker = row.get("marker")
        if marker is None:
            return None
        try:
            return int(marker)
        except (TypeError, ValueError):
            return None

    async def set_marker(self, marker: int | None) -> None:
        # Колонка marker — TEXT; храним строкой (маркер Max — целочисленный курсор).
        await self._db.execute(
            "UPDATE bot_poller_state SET marker = %s, updated_at = now() WHERE id = 1",
            (str(marker) if marker is not None else None,),
        )
