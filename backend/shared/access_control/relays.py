"""Политика управления каналами блока реле шкафа.

Чистые функции без I/O: одна и та же проверка используется при создании
relay-команды, при её подтверждении step-up OTP и для ``allowed_actions``
в ``GET /locker/{id}/relays``.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus

from shared.enums import CommandType, UserRole

from .commands import RELAY_COMMAND_TYPES, check_command_permission

RELAY_CHANNEL_MIN = 1
RELAY_CHANNEL_MAX = 4

_HTTP_FORBIDDEN = HTTPStatus.FORBIDDEN
_HTTP_CONFLICT = HTTPStatus.CONFLICT
_HTTP_UNPROCESSABLE = HTTPStatus.UNPROCESSABLE_ENTITY


class RelayProblem(StrEnum):
    """Машинный код отказа relay-команды (суффикс ProblemDetails.type)."""

    COMMAND_FORBIDDEN = "relay-command-forbidden"
    BLOCK_ABSENT = "relay-block-absent"
    CHANNEL_UNAVAILABLE = "relay-channel-unavailable"
    CHANNEL_PROTECTED = "relay-channel-protected"
    BOARD_OFFLINE = "board-offline"
    COMMAND_IN_PROGRESS = "relay-command-in-progress"
    INVALID_PARAMETERS = "relay-invalid-parameters"


RELAY_PROBLEM_STATUS: dict[RelayProblem, int] = {
    RelayProblem.COMMAND_FORBIDDEN: _HTTP_FORBIDDEN,
    RelayProblem.BLOCK_ABSENT: _HTTP_CONFLICT,
    RelayProblem.CHANNEL_UNAVAILABLE: _HTTP_CONFLICT,
    RelayProblem.CHANNEL_PROTECTED: _HTTP_FORBIDDEN,
    RelayProblem.BOARD_OFFLINE: _HTTP_CONFLICT,
    RelayProblem.COMMAND_IN_PROGRESS: _HTTP_CONFLICT,
    RelayProblem.INVALID_PARAMETERS: _HTTP_UNPROCESSABLE,
}


@dataclass(frozen=True, slots=True)
class LockerRelayPolicy:
    control_enabled: bool
    is_protected: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class RelayDecision:
    denial: RelayProblem | None = None

    @property
    def allowed(self) -> bool:
        return self.denial is None

    @property
    def http_status(self) -> int | None:
        return None if self.denial is None else RELAY_PROBLEM_STATUS[self.denial]


RELAY_ALLOWED = RelayDecision()


def is_relay_command(command_type: CommandType) -> bool:
    return command_type in RELAY_COMMAND_TYPES


def authorize_relay_command(
    role: UserRole,
    command_type: CommandType,
    *,
    has_relays: bool | None,
    relay: LockerRelayPolicy | None,
) -> RelayDecision:
    """Разрешена ли роли relay-команда на канал с данной политикой.

    Порядок: право роли (403, не раскрывает состояние шкафа) → наличие блока
    (409) → доступность канала (409) → защищённый канал только administrator
    (403). ``has_relays=None`` (плата ещё не прислала stat) — отказ.
    """
    if not is_relay_command(command_type) or not check_command_permission(
        role.value, command_type.value,
    ):
        return RelayDecision(RelayProblem.COMMAND_FORBIDDEN)
    if has_relays is not True:
        return RelayDecision(RelayProblem.BLOCK_ABSENT)
    if relay is None or not relay.is_active or not relay.control_enabled:
        return RelayDecision(RelayProblem.CHANNEL_UNAVAILABLE)
    if relay.is_protected and role != UserRole.ADMINISTRATOR:
        return RelayDecision(RelayProblem.CHANNEL_PROTECTED)
    return RELAY_ALLOWED


def allowed_relay_actions(
    role: UserRole,
    *,
    has_relays: bool | None,
    relay: LockerRelayPolicy | None,
) -> list[CommandType]:
    """relay-команды, доступные роли на канале, в порядке on/off/restart."""
    return [
        command_type
        for command_type in (CommandType.RELAY_ON, CommandType.RELAY_OFF, CommandType.RELAY_RESTART)
        if authorize_relay_command(role, command_type, has_relays=has_relays, relay=relay).allowed
    ]
