"""
DTO для Contractor API.

Соответствует OpenAPI схемам:
- CreateContractorRequest (openapi/schemas/requests.yaml)
- UpdateContractorRequest (openapi/schemas/requests.yaml)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CreateContractorDTO:
    """
    DTO для создания нового подрядчика.

    Соответствует OpenAPI схеме CreateContractorRequest.

    Required fields:
        name: Название подрядчика
    """
    name: str

    def __post_init__(self) -> None:
        """Валидация после инициализации dataclass"""
        if not self.name or not self.name.strip():
            raise ValueError("name cannot be empty")


@dataclass
class UpdateContractorDTO:
    """
    DTO для обновления подрядчика.

    Соответствует OpenAPI схеме UpdateContractorRequest.

    Required fields:
        name: Новое название подрядчика
    """
    name: str

    def __post_init__(self) -> None:
        """Валидация после инициализации dataclass"""
        if not self.name or not self.name.strip():
            raise ValueError("name cannot be empty")
