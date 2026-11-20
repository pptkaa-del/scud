from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class NotificationType(StrEnum):
    PERSISTENT = "persistent"


@dataclass(slots=True)
class Notification:
    id: str  # UUID
    type: NotificationType
    text: str
    tags: dict[str, Any]
    start_at: datetime | None
    is_cancelled: bool
    created_at: datetime
    created_by: int | None


@dataclass(slots=True)
class NotificationRecipient:
    id: int
    notification_id: str  # UUID
    user_id: int | None
    role: str | None
