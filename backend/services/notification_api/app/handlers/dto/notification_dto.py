from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ...notification_data.db.models.notification import NotificationType


@dataclass
class CreateNotificationDTO:
    type: str
    text: str
    tags: dict[str, Any] = field(default_factory=dict)
    start_at: str | None = None
    user_ids: list[int] | None = None
    roles: list[str] | None = None

    def __post_init__(self) -> None:
        if not self.text or not self.text.strip():
            raise ValueError("text cannot be empty")

        if self.type == NotificationType.PERSISTENT:
            if not self.start_at:
                raise ValueError("start_at required for persistent")
            if not self.user_ids and not self.roles:
                raise ValueError("user_ids or roles required for persistent")
            _validate_iso8601(self.start_at, "start_at")

        else:
            raise ValueError(f"Invalid notification type: {self.type}")

        if self.tags:
            for k, v in self.tags.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise ValueError("tags must be a dict of string keys and string values")


def _validate_iso8601(value: str, field_name: str) -> None:
    try:
        datetime.fromisoformat(value)
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid {field_name} format: {value}. Must be ISO 8601 datetime") from None
