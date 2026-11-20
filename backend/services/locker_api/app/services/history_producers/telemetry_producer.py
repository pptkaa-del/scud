"""Продьюсер телеметрии в history_api.

Принимает уже резолвленные dict-ы readings (с sensor_type_code) и складывает
их в outbound queue. Резолв sensor_type_code должен быть выполнен в
device_handler ДО enqueue, чтобы не уносить db-соединение запроса в фоновую
задачу.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
import uuid

from ._base import BaseHistoryProducer


def make_reading(
    sensor_id: str,
    sensor_type_code: str,
    value: Any,
) -> dict[str, Any]:
    """Собрать один telemetry-reading для history (единый источник WS+MQTT).

    Числовое значение кладётся в ``value`` (DOUBLE PRECISION в history):
    ``bool`` → 1.0/0.0, ``int``/``float`` → ``float``. Нечисловое непустое
    значение идёт в ``value_text`` (строкой). ``None`` не добавляет ни
    ``value``, ни ``value_text`` (датчик без показания). ``locker_id``,
    ``recorded_at`` и ``event_uid`` проставляет ``publish_batch``.
    """
    reading: dict[str, Any] = {
        "sensor_id": str(sensor_id),
        "sensor_type_code": sensor_type_code,
    }
    if isinstance(value, bool):
        reading["value"] = 1.0 if value else 0.0
    elif isinstance(value, int | float):
        reading["value"] = float(value)
    elif value is not None:
        reading["value_text"] = str(value)
    return reading


class TelemetryProducer(BaseHistoryProducer):
    """Канал `telemetry` → POST /internal/api/v1/telemetry.

    Лимит — 1000 readings за один запрос (см. history_api).
    """

    CHANNEL = "telemetry"
    ENDPOINT = "/internal/api/v1/telemetry"

    async def publish_batch(
        self,
        locker_id: str,
        readings: Iterable[dict[str, Any]],
    ) -> int | None:
        """Публикует батч readings.

        Каждый reading должен содержать `sensor_id`, `sensor_type_code`,
        опционально `value`/`value_text`/`recorded_at`/`metadata`. `locker_id`
        проставляется на уровне реквеста и пробрасывается в каждый reading.

        Если `readings` пуст — ничего не делает.
        """
        readings_list = list(readings)
        if not readings_list:
            return None

        now_iso = datetime.now(UTC).isoformat()
        normalized: list[dict[str, Any]] = []
        for reading in readings_list:
            item = {**reading, "locker_id": locker_id}
            if "recorded_at" not in item:
                item["recorded_at"] = now_iso
            elif isinstance(item["recorded_at"], datetime):
                item["recorded_at"] = item["recorded_at"].isoformat()
            # ARC-02: per-reading event_uid для построчной дедупликации
            # повторно доставленного батча. Генерируется один раз на enqueue
            # и живёт в payload outbox-строки → стабилен между ретраями.
            if "event_uid" not in item:
                item["event_uid"] = str(uuid.uuid4())
            normalized.append(item)

        return await self.enqueue({"locker_id": locker_id, "readings": normalized})
