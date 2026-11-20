"""
DTO для Emergency Request API.

Соответствует OpenAPI схемам:
- CreateEmergencyRequestRequest (openapi/schemas/emergency_request.yaml)
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid


@dataclass
class CreateEmergencyRequestDTO:
    """
    DTO для создания заявки на экстренное открытие шкафа.

    Соответствует OpenAPI схеме CreateEmergencyRequestRequest.

    Required fields:
        locker_id: UUID шкафа для экстренного открытия
    """
    locker_id: str

    def __post_init__(self) -> None:
        """Валидация после инициализации dataclass"""
        if self.locker_id is None:
            raise ValueError("locker_id is required")

        try:
            uuid.UUID(self.locker_id)
        except (ValueError, AttributeError):
            raise ValueError("locker_id must be a valid UUID") from None
