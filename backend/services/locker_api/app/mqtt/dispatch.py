"""Маршрутизация входящих MQTT-сообщений: $SYS-метрики, полосы bulkhead, ingest."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import logging
from typing import Protocol

from shared.enums import RelayMessageOutcome
from shared.metrics import record_relay_message

from . import relay_codec as rc
from .bulkhead import ByteBudgetBulkhead, IngestBulkhead
from .relay_protocol import is_photos_topic, is_rep_topic

logger = logging.getLogger(__name__)

_SYS_PREFIX = "$SYS/"
_PHOTO_HEADER_BYTES = 20


class _Ingest(Protocol):
    async def handle_message(
        self, topic: str, payload: bytes, received_at: datetime | None = None,
    ) -> None: ...


class _SysMetrics(Protocol):
    def on_sys_message(self, topic: str, payload: bytes) -> None: ...


def make_photo_bulkhead(max_concurrency: int, max_pending_bytes: int) -> ByteBudgetBulkhead:
    """Полоса фото; бюджет обязан вмещать кадр максимального размера, иначе он не пройдёт никогда."""
    min_budget = rc.MAX_RELAY_PHOTO_BYTES + _PHOTO_HEADER_BYTES
    if max_pending_bytes < min_budget:
        raise ValueError(
            f"MQTT_INGEST_PHOTO_MAX_PENDING_BYTES={max_pending_bytes} must be >= {min_budget}")
    return ByteBudgetBulkhead(max_concurrency, max_pending_bytes)


def make_relay_dispatch(
    ingest: _Ingest,
    sys_metrics: _SysMetrics,
    bulkhead: IngestBulkhead,
    photo_bulkhead: ByteBudgetBulkhead,
) -> Callable[[str, bytes], Awaitable[None]]:
    async def dispatch(topic: str, payload: bytes) -> None:
        if topic.startswith(_SYS_PREFIX):
            sys_metrics.on_sys_message(topic, payload)
            return
        # До очереди bulkhead: по времени приёма судят об опоздании rep относительно TTL.
        received_at = datetime.now(UTC)

        async def _handle() -> None:
            try:
                await ingest.handle_message(topic, payload, received_at=received_at)
            except Exception:
                record_relay_message(RelayMessageOutcome.HANDLER_ERROR)
                raise

        if is_photos_topic(topic):
            admitted = await photo_bulkhead.run(len(payload), _handle)
            if not admitted:
                logger.warning(
                    "photo frame dropped: pending photo bytes budget exhausted (%d bytes)",
                    len(payload))
        else:
            admitted = await bulkhead.run(is_rep_topic(topic), _handle)
        if not admitted:
            record_relay_message(RelayMessageOutcome.DROPPED_OVERLOAD)

    return dispatch
