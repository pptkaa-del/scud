"""
DTO для User API.

Соответствует OpenAPI схемам:
- CreateUserRequest  (openapi/schemas/requests.yaml)
- UpdateUserRequest  (openapi/schemas/requests.yaml)
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from app.user_data.db.models.user import UserRole

# Паттерн для российского номера телефона: +7 и 10 цифр
PHONE_PATTERN = re.compile(r'^\+7\d{10}$')


@dataclass
class CreateUserDTO:
    """
    DTO для создания нового пользователя.

    Соответствует OpenAPI схеме CreateUserRequest.

    Required fields:
        phone: Номер телефона в формате +79999999999
        full_name: Полное имя пользователя

    Optional fields:
        contractor_id: ID подрядчика (если пользователь привязан к подрядчику)
        role: Роль пользователя (по умолчанию worker)

    Note:
        При использовании с dacite.Config(cast=[UserRole]),
        строки автоматически конвертируются в enum и валидируются.

        Валидация телефона происходит в __post_init__().
    """
    phone: str
    full_name: str
    contractor_id: int | None = None
    role: UserRole = UserRole.WORKER
    password: str | None = None

    def __post_init__(self) -> None:
        """Валидация после инициализации dataclass"""
        # Валидация телефона
        if not self.phone or not PHONE_PATTERN.match(self.phone):
            raise ValueError(
                f"Invalid phone format: {self.phone}. Must be in format +79999999999"
            )

        # Валидация имени
        if not self.full_name or not self.full_name.strip():
            raise ValueError("full_name cannot be empty")

        # Валидация contractor_id
        if self.contractor_id is not None and self.contractor_id <= 0:
            raise ValueError("contractor_id must be a positive integer")

        # Валидация пароля
        if self.password is not None and len(self.password) < 8:
            raise ValueError("password must be at least 8 characters")


@dataclass
class UpdateUserDTO:
    """
    DTO для частичного обновления пользователя (PATCH).

    Соответствует OpenAPI схеме UpdateUserRequest.

    Все поля опциональны — передаются только изменяемые.
    Поле со значением None означает «не изменять».

    Note:
        При использовании с dacite.Config(cast=[UserRole]),
        строки автоматически конвертируются в enum.
    """
    phone: str | None = None
    full_name: str | None = None
    role: UserRole | None = None
    contractor_id: int | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        if self.phone is not None and not PHONE_PATTERN.match(self.phone):
            raise ValueError(
                f"Invalid phone format: {self.phone}. Must be in format +79999999999"
            )

        if self.full_name is not None and not self.full_name.strip():
            raise ValueError("full_name cannot be empty")

        if self.contractor_id is not None and self.contractor_id <= 0:
            raise ValueError("contractor_id must be a positive integer")

        if self.password is not None and len(self.password) < 8:
            raise ValueError("password must be at least 8 characters")

    def has_changes(self) -> bool:
        """Возвращает True если хотя бы одно поле задано для обновления."""
        return any(v is not None for v in (
            self.phone, self.full_name, self.role, self.contractor_id, self.password
        ))
