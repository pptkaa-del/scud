from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from shared.enums import CommandStatus, CommandType

__all__ = ["CommandStatus", "CommandType", "LockerCommand"]


@dataclass(slots=True)
class LockerCommand:
    id: str  # UUID
    locker_id: str  # UUID
    type: CommandType
    status: CommandStatus
    parameters: dict[str, Any]
    created_at: datetime
    created_by: int | None
    sent_at: datetime | None
    delivered_at: datetime | None
    executed_at: datetime | None
    failed_at: datetime | None
    result: dict[str, Any]
    error_message: str | None
    retry_count: int
    max_retries: int
    # uint32, выделяется queue-процессором для MQTT-relay (эхо-идемпотентность
    # rep). NULL для WS-команд и до первой отправки. Должен присутствовать в
    # модели, иначе MqttTransport.prepare_send всегда видит None и перевыделяет
    # relay_cmd_id при повторе после ack-timeout (DB-COALESCE спасает
    # корректность, но порождает лишний UPDATE).
    relay_cmd_id: int | None = None
