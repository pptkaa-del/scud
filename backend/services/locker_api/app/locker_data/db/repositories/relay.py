from __future__ import annotations

from typing import Any
from uuid import UUID

from dacite import Config, from_dict

from shared.access_control import RELAY_COMMAND_TYPES
from shared.database import AsyncPostgres, SqlParam
from shared.enums import CommandStatus, CommandType

from ..models.relay import RELAY_CHANNELS, LockerRelay, RelayPendingCommand

_DACITE_CONFIG = Config(cast=[CommandStatus, CommandType])

_COLS = (
    "locker_id::text AS locker_id, channel, name, purpose, control_enabled, "
    "is_protected, is_active, reported_state, changed_at, created_at, updated_at, updated_by"
)

SETTINGS_COLUMNS: frozenset[str] = frozenset(
    {"name", "purpose", "control_enabled", "is_protected", "is_active"},
)

_RELAY_TYPE_VALUES: list[str] = sorted(t.value for t in RELAY_COMMAND_TYPES)
_ACTIVE_STATUSES: tuple[CommandStatus, ...] = (
    CommandStatus.PENDING, CommandStatus.SENT, CommandStatus.DELIVERED,
)


def _to_relay(row: dict[str, Any]) -> LockerRelay:
    return from_dict(LockerRelay, row)


class RelayRepository:
    """Каналы блока реле (locker_relays). Строки создаются при приёме stat."""

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get(self, locker_id: UUID | str, channel: int) -> LockerRelay | None:
        row = await self._db.fetch_one(
            f"SELECT {_COLS} FROM locker_relays WHERE locker_id = %s AND channel = %s",
            (str(locker_id), channel),
        )
        return _to_relay(row) if row else None

    async def list_for_locker(self, locker_id: UUID | str) -> list[LockerRelay]:
        rows = await self._db.fetch_all(
            f"SELECT {_COLS} FROM locker_relays WHERE locker_id = %s ORDER BY channel",
            (str(locker_id),),
        )
        return [_to_relay(row) for row in rows]

    async def ensure_channels(self, locker_id: UUID | str) -> None:
        await self._db.execute(
            """
            INSERT INTO locker_relays (locker_id, channel)
            SELECT %s, c FROM unnest(%s::smallint[]) AS c
            ON CONFLICT (locker_id, channel) DO NOTHING
            """,
            (str(locker_id), list(RELAY_CHANNELS)),
            idempotent=True,
        )

    async def set_reported(self, locker_id: UUID | str, states: dict[int, int]) -> None:
        """Пишет только изменившиеся каналы; changed_at — момент смены."""
        if not states:
            return
        channels = list(states)
        await self._db.execute(
            """
            UPDATE locker_relays AS r
            SET reported_state = s.state,
                changed_at = now(),
                updated_at = now()
            FROM unnest(%s::smallint[], %s::smallint[]) AS s(channel, state)
            WHERE r.locker_id = %s
              AND r.channel = s.channel
              AND r.reported_state IS DISTINCT FROM s.state
            """,
            (channels, [states[c] for c in channels], str(locker_id)),
            idempotent=True,
        )

    async def update_settings(
        self,
        locker_id: UUID | str,
        channel: int,
        changes: dict[str, object],
        updated_by: int | None,
    ) -> LockerRelay | None:
        sets: list[str] = []
        params: list[SqlParam] = []
        for column, value in changes.items():
            if column not in SETTINGS_COLUMNS:
                raise ValueError(f"unknown relay setting: {column}")
            if value is not None and not isinstance(value, str | bool):
                raise TypeError(f"invalid value for {column}")
            sets.append(f"{column} = %s")
            params.append(value)
        if not sets:
            return await self.get(locker_id, channel)
        params.extend([updated_by, str(locker_id), channel])
        row = await self._db.fetch_one(
            f"""
            UPDATE locker_relays
            SET {', '.join(sets)}, updated_at = now(), updated_by = %s
            WHERE locker_id = %s AND channel = %s
            RETURNING {_COLS}
            """,
            tuple(params),
            read_only=False,
        )
        return _to_relay(row) if row else None

    async def pending_commands(self, locker_id: UUID | str) -> dict[int, RelayPendingCommand]:
        """Активные relay-команды шкафа по каналу (на канал — не больше одной)."""
        rows = await self._db.fetch_all(
            """
            SELECT id::text AS id, type, status, (parameters ->> 'relay')::int AS channel
            FROM locker_commands
            WHERE locker_id = %s
              AND type = ANY(%s::command_type[])
              AND status = ANY(%s::command_status[])
              AND parameters ->> 'relay' ~ '^[1-4]$'
            ORDER BY created_at
            """,
            (
                str(locker_id),
                _RELAY_TYPE_VALUES,
                [s.value for s in _ACTIVE_STATUSES],
            ),
        )
        result: dict[int, RelayPendingCommand] = {}
        for row in rows:
            cmd = from_dict(RelayPendingCommand, row, config=_DACITE_CONFIG)
            result[cmd.channel] = cmd
        return result
