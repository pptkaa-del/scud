from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from shared.enums import CommandStatus, CommandType

RELAY_CHANNELS: tuple[int, ...] = (1, 2, 3, 4)


class RelayState(IntEnum):
    OFF = 0
    ON = 1
    ERROR = 2


@dataclass(slots=True)
class LockerRelay:
    locker_id: str
    channel: int
    name: str | None
    purpose: str | None
    control_enabled: bool
    is_protected: bool
    is_active: bool
    reported_state: int | None
    changed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    updated_by: int | None


@dataclass(slots=True)
class RelayPendingCommand:
    id: str
    type: CommandType
    status: CommandStatus
    channel: int
