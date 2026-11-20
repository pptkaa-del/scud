"""Проверки relay-команды, общие для создания и подтверждения step-up OTP."""
from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from dacite import Config, DaciteError, from_dict

from shared.access_control import (
    RELAY_CHANNEL_MAX,
    RELAY_CHANNEL_MIN,
    RELAY_PROBLEM_STATUS,
    LockerRelayPolicy,
    RelayProblem,
    authorize_relay_command,
)
from shared.database import AsyncPostgres
from shared.enums import CommandType, UserRole
from shared.http import ProblemTitle

from ..locker_data.db.models.locker import Connectivity, Locker
from ..locker_data.db.repositories.relay import RelayRepository

PROBLEM_TYPE_BASE = "https://api.scuddocs.com/problems/"
_PROBLEM_TITLES: dict[int, str] = {
    HTTPStatus.FORBIDDEN: ProblemTitle.FORBIDDEN,
    HTTPStatus.CONFLICT: ProblemTitle.CONFLICT,
    HTTPStatus.UNPROCESSABLE_ENTITY: HTTPStatus.UNPROCESSABLE_ENTITY.phrase,
}
_STRICT = Config(strict=True)


@dataclass(frozen=True, slots=True)
class RelayCommandParameters:
    relay: int


@dataclass(frozen=True, slots=True)
class RelayProblemResponse:
    type_uri: str
    title: str
    status: int
    detail: str


def relay_problem(problem: RelayProblem, detail: str) -> RelayProblemResponse:
    status = RELAY_PROBLEM_STATUS[problem]
    return RelayProblemResponse(
        type_uri=f"{PROBLEM_TYPE_BASE}{problem.value}",
        title=_PROBLEM_TITLES[status],
        status=status,
        detail=detail,
    )


def parse_relay_parameters(raw: object) -> RelayCommandParameters | None:
    """Строго ``{"relay": int 1..4}``; bool не считается int."""
    if not isinstance(raw, dict):
        return None
    try:
        params = from_dict(RelayCommandParameters, raw, config=_STRICT)
    except DaciteError:
        return None
    if isinstance(params.relay, bool) or not RELAY_CHANNEL_MIN <= params.relay <= RELAY_CHANNEL_MAX:
        return None
    return params


async def check_relay_command(
    db: AsyncPostgres,
    *,
    role: str,
    locker: Locker,
    command_type: CommandType,
    parameters: Any,
) -> RelayCommandParameters | RelayProblemResponse:
    """Все проверки relay-команды; политика канала читается из БД заново."""
    params = parse_relay_parameters(parameters)
    if params is None:
        return relay_problem(
            RelayProblem.INVALID_PARAMETERS,
            f"parameters must be {{\"relay\": {RELAY_CHANNEL_MIN}..{RELAY_CHANNEL_MAX}}}",
        )
    try:
        user_role = UserRole(role)
    except ValueError:
        return relay_problem(RelayProblem.COMMAND_FORBIDDEN, "Role does not permit relay commands")
    if locker.has_relays is not True:
        return relay_problem(RelayProblem.BLOCK_ABSENT, "Locker has no relay block")
    if locker.connectivity != Connectivity.ONLINE:
        return relay_problem(RelayProblem.BOARD_OFFLINE, "Locker board is offline")
    row = await RelayRepository(db).get(locker.id, params.relay)
    policy = (
        LockerRelayPolicy(
            control_enabled=row.control_enabled,
            is_protected=row.is_protected,
            is_active=row.is_active,
        )
        if row is not None
        else None
    )
    decision = authorize_relay_command(
        user_role, command_type, has_relays=locker.has_relays, relay=policy,
    )
    if decision.denial is not None:
        return relay_problem(
            decision.denial, f"Relay {params.relay}: {decision.denial.value}",
        )
    return params
