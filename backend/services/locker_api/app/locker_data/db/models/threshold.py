from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class SensorThreshold:
    id: int
    sensor_id: str  # UUID
    min_value: float | None
    max_value: float | None
    fixed_value: str | None
    created_at: datetime
    created_by: int | None
    updated_at: datetime
    updated_by: int | None
    is_active: bool
    description: str | None
