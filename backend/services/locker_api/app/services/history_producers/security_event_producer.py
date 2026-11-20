"""Продьюсеры security events: threshold_violation / intrusion / door_tamper /
command_late_execution.

Все используют один и тот же эндпойнт history_api
(`/internal/api/v1/events/security`), но различаются `event_type` и
`severity`:

  - ThresholdViolationProducer  → event_type=threshold_violation, severity=high.
                                  Дефолт для всех датчиков, кроме intrusion/tamper.
  - IntrusionProducer           → event_type=intrusion, severity=high.
                                  Срабатывает на sensor_type_code='intrusion'.
  - DoorTamperProducer          → event_type=door_tamper, severity=critical.
                                  Срабатывает на sensor_type_code='tamper'.
  - CommandLateExecutionProducer → event_type=command_late_execution, severity=high.
                                  SUCCESS-rep по уже терминальной команде (mqtt.results).

Маршрутизация по sensor_type_code — в state_monitor (см. dispatch_alarm).
"""

from __future__ import annotations

import logging
from typing import Any
import uuid

from shared.database import AsyncPostgres
from shared.enums import EventSeverity, SecurityEventType

from ...locker_data.db.models.sensor_type import SensorCode
from ._base import BaseHistoryProducer

# Security-events метрика (B3). Изолировано try/except, чтобы старый build без
# python3-prometheus-client не падал на импорте (зеркалит device_handler).
try:
    from shared.metrics import SERVICE_NAME as _METRICS_SERVICE_NAME, security_events_total
    _SECURITY_METRICS_AVAILABLE = True
except Exception:  # pragma: no cover - метрики опциональны
    _SECURITY_METRICS_AVAILABLE = False

logger = logging.getLogger(__name__)

SENSOR_TYPE_INTRUSION = SensorCode.INTRUSION
SENSOR_TYPE_TAMPER = SensorCode.TAMPER


class _BaseSecurityEventProducer(BaseHistoryProducer):
    """Общая инфраструктура для всех security_event-продьюсеров."""

    CHANNEL = "security_event"
    ENDPOINT = "/internal/api/v1/events/security"

    EVENT_TYPE: str = ""
    SEVERITY: str = ""

    async def publish(
        self,
        locker_id: str,
        sensor_id: str | None,
        violations: list[str],
        *,
        sensor_type_code: str | None = None,
        raw_value: Any = None,
        event_uid: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> int:
        payload = self.build_payload(
            locker_id, sensor_id, violations, sensor_type_code=sensor_type_code,
            raw_value=raw_value, event_uid=event_uid, extra_metadata=extra_metadata,
        )
        self.record_enqueued()
        return await self.enqueue(payload)

    def build_payload(
        self,
        locker_id: str,
        sensor_id: str | None,
        violations: list[str],
        *,
        sensor_type_code: str | None = None,
        raw_value: Any = None,
        event_uid: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Payload outbox-строки без постановки — для вызывающих со своим INSERT (relay-серии)."""
        if not self.EVENT_TYPE or not self.SEVERITY:
            raise RuntimeError(
                f"{type(self).__name__} must define EVENT_TYPE and SEVERITY",
            )
        metadata: dict[str, Any] = dict(extra_metadata or {})
        if sensor_type_code is not None:
            metadata["sensor_type_code"] = sensor_type_code
        if raw_value is not None:
            metadata["raw_value"] = raw_value

        payload: dict[str, Any] = {
            "locker_id": str(locker_id),
            "event_type": self.EVENT_TYPE,
            "severity": self.SEVERITY,
            "violated_thresholds": [{"description": v} for v in violations],
            # ARC-02: стабильный event_uid в payload outbox-строки — при
            # ретрае доставки переиспользуется тот же uid (payload не
            # перегенерируется), history_api делает ON CONFLICT DO NOTHING.
            # event_uid можно передать явно (например, relay-фото-серия
            # передаёт детерминированный uuid5 — редоставка серии не должна
            # плодить второй intrusion event); по умолчанию — uuid4 как раньше.
            "event_uid": event_uid if event_uid is not None else str(uuid.uuid4()),
        }
        if sensor_id is not None:
            payload["sensor_id"] = str(sensor_id)
        if metadata:
            payload["metadata"] = metadata
        return payload

    def record_enqueued(self) -> None:
        if not _SECURITY_METRICS_AVAILABLE:
            return
        try:
            security_events_total.labels(
                service=_METRICS_SERVICE_NAME,
                type=self.EVENT_TYPE,
                severity=self.SEVERITY,
            ).inc()
        except Exception:  # pragma: no cover - метрика не должна ронять enqueue
            logger.debug("Failed to increment security_events_total", exc_info=True)


class ThresholdViolationProducer(_BaseSecurityEventProducer):
    EVENT_TYPE = SecurityEventType.THRESHOLD_VIOLATION.value
    SEVERITY = EventSeverity.HIGH.value


class IntrusionProducer(_BaseSecurityEventProducer):
    EVENT_TYPE = SecurityEventType.INTRUSION.value
    SEVERITY = EventSeverity.HIGH.value


class DoorTamperProducer(_BaseSecurityEventProducer):
    EVENT_TYPE = SecurityEventType.DOOR_TAMPER.value
    SEVERITY = EventSeverity.CRITICAL.value


class CommandLateExecutionProducer(_BaseSecurityEventProducer):
    EVENT_TYPE = SecurityEventType.COMMAND_LATE_EXECUTION.value
    SEVERITY = EventSeverity.HIGH.value


def producer_for_sensor_type(db: AsyncPostgres, sensor_type_code: str | None) -> _BaseSecurityEventProducer:
    """Выбрать продьюсер по `sensor_type_code` датчика.

    `intrusion` → IntrusionProducer, `tamper` → DoorTamperProducer,
    всё остальное → ThresholdViolationProducer.
    """
    if sensor_type_code == SENSOR_TYPE_INTRUSION:
        return IntrusionProducer(db)
    if sensor_type_code == SENSOR_TYPE_TAMPER:
        return DoorTamperProducer(db)
    return ThresholdViolationProducer(db)
