"""Матрица прав по типу команды устройства.

ACM URL-level rule (``acm/app/rules.py``) пропускает любого worker'а с
Допуском на ``POST /api/v1/locker/{id}/commands``. Этот модуль — следующий
уровень: какие command_type каждая роль вправе отправлять.

Используется из ``locker_api/handlers/command.py``. Чистая Python-функция,
не делает сетевых вызовов и не зависит от Tornado.

Матрица:

* ``worker`` — `OPEN`, `CLOSE` (ежедневные операционные действия).
* ``operator`` — `OPEN`, `CLOSE`, `REBOOT`, `RELAY_*` (operations engineer).
* ``administrator`` — `OPEN`, `CLOSE`, `REBOOT`, `RELAY_*` (полный доступ).

Политика конкретного канала реле (защищённый / отключённый) — в
``shared.access_control.relays``.

``CONFIG`` выведен из эксплуатации: ни одна роль не вправе его отправлять
(``check_command_permission`` → False → 403). Значение остаётся в
``CommandType`` только для десериализации legacy-строк в истории команд.

Безопасный default: при любой неизвестной роли (включая пустую строку
или ``None``) или неизвестном command_type — отказ.
"""
from __future__ import annotations

from shared.enums import CommandType, UserRole

RELAY_COMMAND_TYPES: frozenset[CommandType] = frozenset(
    {CommandType.RELAY_ON, CommandType.RELAY_OFF, CommandType.RELAY_RESTART}
)
_RELAY_COMMAND_VALUES: frozenset[str] = frozenset(t.value for t in RELAY_COMMAND_TYPES)

COMMAND_PERMISSION_MATRIX: dict[str, frozenset[str]] = {
    UserRole.WORKER.value: frozenset(
        {CommandType.OPEN.value, CommandType.CLOSE.value}
    ),
    UserRole.OPERATOR.value: frozenset(
        {
            CommandType.OPEN.value,
            CommandType.CLOSE.value,
            CommandType.REBOOT.value,
            *_RELAY_COMMAND_VALUES,
        }
    ),
    UserRole.ADMINISTRATOR.value: frozenset(
        {
            CommandType.OPEN.value,
            CommandType.CLOSE.value,
            CommandType.REBOOT.value,
            *_RELAY_COMMAND_VALUES,
        }
    ),
}

# Step-up OTP при require_2fa=true. close — без второго фактора:
# принудительное закрытие не должно ждать.
OTP_REQUIRED_COMMANDS: frozenset[str] = frozenset(
    {CommandType.OPEN.value, CommandType.REBOOT.value, *_RELAY_COMMAND_VALUES}
)


def check_command_permission(role: str, command_type: str) -> bool:
    """Возвращает True если роль вправе отправить данный command_type.

    Args:
        role: значение роли из X-Remote-Role (выставляется ACM). Пустая
            строка / None / unknown → False (default deny).
        command_type: строковое значение CommandType (open/close/reboot).
            Unknown / retired command (config) → False.

    Returns:
        True если разрешено. False иначе.
    """
    if not role:
        return False
    allowed = COMMAND_PERMISSION_MATRIX.get(role, frozenset())
    return command_type in allowed
