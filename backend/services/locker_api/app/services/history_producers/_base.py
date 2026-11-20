"""Базовый класс продьюсеров истории.

Доменные продьюсеры (TelemetryProducer, CommandHistoryProducer,
OfflineEventProducer, ThresholdViolationProducer, IntrusionProducer,
DoorTamperProducer, MediaProducer) определяют только `CHANNEL`,
`ENDPOINT` и метод формирования payload, всё остальное — здесь.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from shared.database import AsyncPostgres

from ...locker_data.db.repositories.outbound import OutboundRepository

logger = logging.getLogger(__name__)


class BaseHistoryProducer:
    """Минимальный продьюсер: ставит payload в outbound queue."""

    CHANNEL: ClassVar[str] = ""
    ENDPOINT: ClassVar[str] = ""

    def __init__(self, db: AsyncPostgres) -> None:
        if not self.CHANNEL or not self.ENDPOINT:
            raise RuntimeError(
                f"{type(self).__name__} must define CHANNEL and ENDPOINT class attrs",
            )
        self._db = db
        self._outbound = OutboundRepository(db)

    async def enqueue(
        self, payload: dict[str, Any], *, delay_seconds: float = 0.0
    ) -> int:
        """Поставить событие в очередь. Возвращает id записи.

        Привязка к локеру (FK ON DELETE CASCADE) проставляется
        ``OutboundRepository.enqueue`` автоматически из ``payload['locker_id']`` —
        все продьюсеры кладут его в payload, поэтому отдельно прокидывать не надо.
        """
        event_id = await self._outbound.enqueue(
            self.CHANNEL, self.ENDPOINT, payload, delay_seconds=delay_seconds,
        )
        logger.debug(
            "Enqueued %s event #%d → %s", self.CHANNEL, event_id, self.ENDPOINT,
        )
        return event_id
