from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Contractor:
    id: int
    name: str
    created_at: datetime
    created_by: int | None
