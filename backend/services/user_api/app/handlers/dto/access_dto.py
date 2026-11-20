"""
DTO для Access API.

Соответствует OpenAPI схемам:
- CreateUserAccessRequest (openapi/schemas/access.yaml)
- UpdateUserAccessRequest (openapi/schemas/access.yaml)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.sentinel import UNSET, Unset


def _validate_iso8601_datetime(value: str, field_name: str) -> None:
    """
    Валидация ISO 8601 datetime строки.

    Args:
        value: Строка с датой и временем
        field_name: Имя поля для сообщения об ошибке

    Raises:
        ValueError: Если формат строки не соответствует ISO 8601
    """
    try:
        datetime.fromisoformat(value)
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid {field_name} format: {value}. Must be ISO 8601 datetime") from None


@dataclass
class CreateUserAccessDTO:
    """
    DTO для создания нового допуска пользователя.

    Соответствует OpenAPI схеме CreateUserAccessRequest.
    """
    start_at: str  # ISO 8601 datetime string
    full_access: bool
    end_at: str | None = None  # ISO 8601 datetime string
    request_id: int | None = None
    locker_ids: list[str] | None = field(default_factory=list)

    def __post_init__(self) -> None:
        """Валидация после инициализации dataclass"""
        # Валидация start_at
        if not self.start_at:
            raise ValueError("start_at is required")

        _validate_iso8601_datetime(self.start_at, "start_at")

        # Валидация end_at если указан
        if self.end_at:
            _validate_iso8601_datetime(self.end_at, "end_at")

        # Валидация full_access и locker_ids
        if not self.full_access and (not self.locker_ids or len(self.locker_ids) == 0):
            raise ValueError(
                "locker_ids is required when full_access=false. "
                "Must specify at least one locker ID for limited access"
            )

        # Валидация request_id
        if self.request_id is not None and self.request_id <= 0:
            raise ValueError("request_id must be a positive integer")


@dataclass
class BatchAccessRequestDTO:
    """
    DTO для батч-запроса допусков по нескольким пользователям.

    Соответствует OpenAPI схеме BatchAccessRequest.
    """
    user_ids: list[int]
    include_inactive: bool = False

    def __post_init__(self) -> None:
        if not self.user_ids:
            raise ValueError("user_ids must not be empty")
        if len(self.user_ids) > 100:
            raise ValueError("user_ids must not exceed 100 items")
        if any(not isinstance(uid, int) or uid <= 0 for uid in self.user_ids):
            raise ValueError("each user_id must be a positive integer")


@dataclass
class UpdateUserAccessDTO:
    """
    DTO для обновления допуска пользователя.

    Соответствует OpenAPI схеме UpdateUserAccessRequest.
    """
    start_at: str | None = None  # ISO 8601 datetime string
    end_at: str | None | Unset = UNSET   # UNSET = не передано; None = явный null (удалить срок)
    locker_ids: list[str] | None = None

    def __post_init__(self) -> None:
        """Валидация после инициализации dataclass"""
        # Валидация start_at если указан
        if self.start_at:
            _validate_iso8601_datetime(self.start_at, "start_at")

        # Валидация end_at если указан (UNSET sentinel falsy, str truthy)
        if isinstance(self.end_at, str) and self.end_at:
            _validate_iso8601_datetime(self.end_at, "end_at")
