"""Продьюсер OFFLINE security event'ов в history_api.

Шлётся при потере связи с устройством (MQTT-реле offline sweep). metadata
содержит:
  - reason: "heartbeat_timeout"
  - last_seen_at: ISO 8601 — последняя зафиксированная активность

severity = "medium" по решению из плана: offline сам по себе не инцидент
безопасности, но должен быть в истории для анализа доступности.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any
import uuid

from shared.enums import EventSeverity, SecurityEventType

from ._base import BaseHistoryProducer

# Security-events метрика (B3). Изолировано try/except — старый build без
# python3-prometheus-client не должен падать на импорте.
try:
    from shared.metrics import SERVICE_NAME as _METRICS_SERVICE_NAME, security_events_total
    _SECURITY_METRICS_AVAILABLE = True
except Exception:  # pragma: no cover - метрики опциональны
    _SECURITY_METRICS_AVAILABLE = False

logger = logging.getLogger(__name__)

REASON_HEARTBEAT_TIMEOUT = "heartbeat_timeout"

_VALID_REASONS = frozenset({REASON_HEARTBEAT_TIMEOUT})


class OfflineEventProducer(BaseHistoryProducer):
    """Канал `security_event` (subset offline) → POST /internal/api/v1/events/security."""

    CHANNEL = "security_event"
    ENDPOINT = "/internal/api/v1/events/security"

    async def publish_offline(
        self,
        locker_id: str,
        reason: str,
        last_seen_at: datetime | None = None,
    ) -> int:
        if reason not in _VALID_REASONS:
            raise ValueError(f"Unknown offline reason: {reason!r}")

        metadata: dict[str, Any] = {"reason": reason}
        if last_seen_at is not None:
            metadata["last_seen_at"] = last_seen_at.isoformat()

        payload = {
            "locker_id": str(locker_id),
            "event_type": SecurityEventType.OFFLINE.value,
            "severity": EventSeverity.MEDIUM.value,
            "metadata": metadata,
            # ARC-02: стабильный event_uid в payload outbox-строки — offline
            # replay (переподключение реле / повторная outbox-доставка) и есть
            # главный кейс дублей, на который нацелен ON CONFLICT по
            # security_events(event_uid). uid генерится один раз на enqueue и
            # живёт в payload → переиспользуется между ретраями.
            "event_uid": str(uuid.uuid4()),
        }
        if _SECURITY_METRICS_AVAILABLE:
            try:
                security_events_total.labels(
                    service=_METRICS_SERVICE_NAME,
                    type=SecurityEventType.OFFLINE.value,
                    severity=EventSeverity.MEDIUM.value,
                ).inc()
            except Exception:  # pragma: no cover - метрика не должна ронять enqueue
                logger.debug("Failed to increment security_events_total", exc_info=True)
        return await self.enqueue(payload)
