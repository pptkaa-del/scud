from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from shared.enums import CommandStatus, CommandType

__all__ = ["CommandStatus", "CommandType", "CommandHistory"]


@dataclass(slots=True)
class CommandHistory:
    """
    Запись истории команды.

    Хранит историю всех отправленных команд с денормализованной информацией
    о пользователях и шкафах для быстрого формирования отчетов.

    Attributes:
        id: ID записи (BIGSERIAL)
        command_id: UUID команды из locker_commands
        locker_id: UUID шкафа
        locker_inventory_number: Инвентарный номер шкафа (денормализация)
        command_type: Тип команды ('open', 'close', 'reboot', 'config')
        parameters: Параметры команды (JSON)
        created_by: ID пользователя, создавшего команду
        user_phone: Телефон пользователя (денормализация)
        user_full_name: ФИО пользователя (денормализация)
        created_at: Время создания команды
        sent_at: Время отправки команды
        executed_at: Время выполнения команды
        failed_at: Время ошибки выполнения
        status: Статус команды
        result: Результат выполнения (JSON)
        error_message: Сообщение об ошибке
    """
    id: int
    command_id: str  # UUID
    locker_id: str  # UUID
    locker_inventory_number: str | None
    command_type: CommandType
    parameters: dict[str, Any]
    created_by: int | None
    user_phone: str | None
    user_full_name: str | None
    created_at: datetime
    sent_at: datetime | None
    executed_at: datetime | None
    failed_at: datetime | None
    status: CommandStatus
    result: dict[str, Any]
    error_message: str | None
