from __future__ import annotations

from dataclasses import dataclass, fields
import logging
from typing import Any

from dacite import Config, DaciteError, from_dict

from shared.access_control import LockerRelayPolicy, allowed_relay_actions
from shared.enums import UserRole
from shared.http import ProblemTitle, ProblemType

from ..locker_data.db.models.relay import LockerRelay, RelayPendingCommand
from ..locker_data.db.repositories.locker import LockerRepository
from ..locker_data.db.repositories.relay import RelayRepository
from .base import BaseHandler

logger = logging.getLogger(__name__)

_HTTP_UNPROCESSABLE = 422
_NAME_MAX_LENGTH = 100
_NULLABLE_SETTINGS = frozenset({"name", "purpose"})


@dataclass(frozen=True, slots=True)
class RelayUpdateDTO:
    name: str | None = None
    purpose: str | None = None
    control_enabled: bool | None = None
    is_protected: bool | None = None
    is_active: bool | None = None

    def __post_init__(self) -> None:
        if self.name is not None and len(self.name) > _NAME_MAX_LENGTH:
            raise ValueError(f"name must be at most {_NAME_MAX_LENGTH} characters")


_DTO_FIELDS = frozenset(f.name for f in fields(RelayUpdateDTO))


def _parse_role(raw: str) -> UserRole | None:
    try:
        return UserRole(raw.strip().lower())
    except ValueError:
        return None


def _policy(relay: LockerRelay) -> LockerRelayPolicy:
    return LockerRelayPolicy(
        control_enabled=relay.control_enabled,
        is_protected=relay.is_protected,
        is_active=relay.is_active,
    )


def relay_item(
    relay: LockerRelay,
    *,
    role: UserRole | None,
    has_relays: bool | None,
    pending: RelayPendingCommand | None,
) -> dict[str, Any]:
    actions = [] if role is None else [
        t.value for t in allowed_relay_actions(role, has_relays=has_relays, relay=_policy(relay))
    ]
    return {
        "channel": relay.channel,
        "name": relay.name,
        "purpose": relay.purpose,
        "control_enabled": relay.control_enabled,
        "is_protected": relay.is_protected,
        "is_active": relay.is_active,
        "reported_state": relay.reported_state,
        "changed_at": relay.changed_at,
        "allowed_actions": actions,
        "pending_command": None if pending is None else {
            "id": pending.id, "type": pending.type.value, "status": pending.status.value,
        },
    }


def parse_relay_update(body: dict[str, Any]) -> dict[str, object]:
    """Изменения из тела PATCH: только присланные поля; null — лишь у name/purpose."""
    dto = from_dict(RelayUpdateDTO, body, config=Config(strict=True))
    changes: dict[str, object] = {}
    for key in body:
        value = getattr(dto, key)
        if value is None and key not in _NULLABLE_SETTINGS:
            raise ValueError(f"{key} must not be null")
        changes[key] = value
    if not changes:
        raise ValueError(f"at least one of {sorted(_DTO_FIELDS)} is required")
    return changes


class RelayListHandler(BaseHandler):
    async def get(self, locker_id: str) -> None:
        self.parse_uuid_or_404(locker_id, what="Locker")
        if not self.ensure_locker_grant(locker_id):
            return
        locker = await LockerRepository(self.db).get_by_id(locker_id)
        if locker is None:
            self.write_not_found("Locker not found")
            return
        repo = RelayRepository(self.db)
        relays = await repo.list_for_locker(locker.id)
        pending = await repo.pending_commands(locker.id) if relays else {}
        role = _parse_role(self.remote_role)
        self.write_json({
            "locker_id": locker.id,
            "has_relays": locker.has_relays,
            "board_version": locker.board_version,
            "connectivity": locker.connectivity.value,
            "reported_at": locker.last_seen_at,
            "items": [
                relay_item(r, role=role, has_relays=locker.has_relays,
                           pending=pending.get(r.channel))
                for r in relays
            ],
        })


class RelayDetailHandler(BaseHandler):
    async def patch(self, locker_id: str, channel_raw: str) -> None:
        self.parse_uuid_or_404(locker_id, what="Locker")
        role = _parse_role(self.remote_role)
        if role != UserRole.ADMINISTRATOR:
            self.write_forbidden("Only administrator can change relay channel settings")
            return
        channel = self.get_int_path_arg(channel_raw, "channel")
        try:
            changes = parse_relay_update(self.get_json_body())
        except (DaciteError, ValueError, TypeError) as exc:
            self.write_problem(
                ProblemType.VALIDATION_ERROR, ProblemTitle.VALIDATION_ERROR,
                _HTTP_UNPROCESSABLE, str(exc),
            )
            return
        locker = await LockerRepository(self.db).get_by_id(locker_id)
        if locker is None:
            self.write_not_found("Locker not found")
            return
        repo = RelayRepository(self.db)
        relay = await repo.update_settings(locker.id, channel, changes, self.current_user_id)
        if relay is None:
            self.write_not_found("Relay channel not found")
            return
        logger.info("relay channel settings changed: locker=%s channel=%d by=%s fields=%s",
                    locker.id, channel, self.current_user_id, sorted(changes))
        pending = (await repo.pending_commands(locker.id)).get(channel)
        self.write_json(relay_item(relay, role=role, has_relays=locker.has_relays,
                                   pending=pending))
