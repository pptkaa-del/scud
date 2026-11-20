"""
DTO для Command History API.

Соответствует OpenAPI схеме:
- CommandHistoryCreate (openapi/schemas/command_history.yaml:69)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...history_data.db.models.command_history import CommandStatus, CommandType


@dataclass
class CommandHistoryCreateDTO:
    """
    DTO для создания записи истории команды.

    Соответствует OpenAPI схеме CommandHistoryCreate.

    Required fields:
        command_id: UUID команды из locker_commands
        locker_id: UUID шкафа
        command_type: Тип команды (автоматически конвертируется из строки в CommandType enum)
        status: Статус команды (автоматически конвертируется из строки в CommandStatus enum)

    Optional fields (денормализованные данные):
        locker_inventory_number: Инвентарный номер шкафа
        parameters: Параметры команды (JSON)
        created_by: ID пользователя, создавшего команду
        user_phone: Телефон пользователя (для отчетов)
        user_full_name: ФИО пользователя (для отчетов)
        result: Результат выполнения (JSON)
        error_message: Сообщение об ошибке (если failed)

    Note:
        При использовании с dacite.Config(cast=[CommandType, CommandStatus]),
        строки автоматически конвертируются в enum и валидируются.
    """
    command_id: str  # UUID
    locker_id: str   # UUID
    command_type: CommandType  # Автоматическая конвертация и валидация
    status: CommandStatus      # Автоматическая конвертация и валидация
    locker_inventory_number: str | None = None
    parameters: dict[str, Any] | None = field(default_factory=dict)
    created_by: int | None = None
    user_phone: str | None = None
    user_full_name: str | None = None
    result: dict[str, Any] | None = field(default_factory=dict)
    error_message: str | None = None
