from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class Connectivity(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class DoorState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


class OperationalStatus(StrEnum):
    NORMAL = "normal"
    MAINTENANCE = "maintenance"
    ALARM = "alarm"


class LockerTransport(StrEnum):
    WS = "ws"
    MQTT = "mqtt"


class LockerView(StrEnum):
    """Режим представления шкафа в списочных/батч-ответах.

    Опускание view в запросе означает полный объект Locker.
    """
    BRIEF = "brief"
    LOCATION = "location"


@dataclass(slots=True)
class Locker:
    id: str  # UUID
    inventory_number: str
    location_address: str | None
    location_lat: float | None
    location_lon: float | None
    created_at: datetime
    created_by: int | None
    updated_at: datetime
    updated_by: int | None
    connectivity: Connectivity
    door: DoorState
    operational_status: OperationalStatus
    last_seen_at: datetime | None
    last_opened_at: datetime | None
    who_open_last: int | None
    description: str | None
    metadata: dict[str, Any]
    public_key: str | None = None
    transport: LockerTransport = LockerTransport.MQTT
    relay_id: int | None = None
    board_version: int | None = None
    has_relays: bool | None = None


@dataclass(slots=True)
class LockerBrief:
    id: str
    inventory_number: str


@dataclass(slots=True)
class LockerLocation:
    id: str
    location_lat: float | None
    location_lon: float | None
    location_address: str | None
    operational_status: OperationalStatus
