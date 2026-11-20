import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.locker_data.db.repositories.locker import LockerRepository
from app.locker_data.db.repositories.relay import RelayRepository
from shared.enums import CommandStatus, CommandType

LOCKER_ID = "550e8400-e29b-41d4-a716-446655440000"
NOW = datetime(2026, 11, 5, tzinfo=UTC)


def _db():
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock()
    return db


def _row(channel=1, **overrides):
    row = {
        "locker_id": LOCKER_ID, "channel": channel, "name": None, "purpose": None,
        "control_enabled": True, "is_protected": False, "is_active": True,
        "reported_state": None, "changed_at": None, "created_at": NOW, "updated_at": NOW,
        "updated_by": None,
    }
    row.update(overrides)
    return row


def test_list_for_locker_maps_rows():
    db = _db()
    db.fetch_all.return_value = [_row(1), _row(2, reported_state=1)]
    relays = asyncio.run(RelayRepository(db).list_for_locker(LOCKER_ID))
    assert [r.channel for r in relays] == [1, 2]
    assert relays[1].reported_state == 1


def test_ensure_channels_inserts_all_four():
    db = _db()
    asyncio.run(RelayRepository(db).ensure_channels(LOCKER_ID))
    sql, params = db.execute.await_args.args
    assert "ON CONFLICT" in sql
    assert params == (LOCKER_ID, [1, 2, 3, 4])


def test_set_reported_only_changed_rows():
    db = _db()
    asyncio.run(RelayRepository(db).set_reported(LOCKER_ID, {1: 1, 3: 2}))
    sql, params = db.execute.await_args.args
    assert "IS DISTINCT FROM" in sql
    assert params == ([1, 3], [1, 2], LOCKER_ID)


def test_set_reported_empty_is_noop():
    db = _db()
    asyncio.run(RelayRepository(db).set_reported(LOCKER_ID, {}))
    db.execute.assert_not_awaited()


def test_update_settings_builds_whitelisted_update():
    db = _db()
    db.fetch_one.return_value = _row(2, name="Свет")
    relay = asyncio.run(RelayRepository(db).update_settings(
        LOCKER_ID, 2, {"name": "Свет", "is_active": False}, 7))
    assert relay is not None
    assert relay.name == "Свет"
    sql, params = db.fetch_one.await_args.args
    assert "name = %s" in sql
    assert "is_active = %s" in sql
    assert params == ("Свет", False, 7, LOCKER_ID, 2)


@pytest.mark.parametrize("changes", [{"channel": 3}, {"name; DROP TABLE x": "a"}])
def test_update_settings_rejects_unknown_columns(changes):
    with pytest.raises(ValueError):
        asyncio.run(RelayRepository(_db()).update_settings(LOCKER_ID, 1, changes, 7))


def test_update_settings_rejects_bad_value_type():
    with pytest.raises(TypeError):
        asyncio.run(RelayRepository(_db()).update_settings(LOCKER_ID, 1, {"is_active": 1.5}, 7))


def test_pending_commands_keyed_by_channel():
    db = _db()
    db.fetch_all.return_value = [
        {"id": "c1", "type": "relay_on", "status": "sent", "channel": 2},
    ]
    pending = asyncio.run(RelayRepository(db).pending_commands(LOCKER_ID))
    assert pending[2].type is CommandType.RELAY_ON
    assert pending[2].status is CommandStatus.SENT
    assert set(pending) == {2}


def test_update_board_info_reports_change():
    db = _db()
    db.fetch_one.return_value = {"id": LOCKER_ID}
    assert asyncio.run(LockerRepository(db).update_board_info(LOCKER_ID, 2, True)) is True
    sql, _ = db.fetch_one.await_args.args
    assert "IS DISTINCT FROM" in sql
    db.fetch_one.return_value = None
    assert asyncio.run(LockerRepository(db).update_board_info(LOCKER_ID, 2, True)) is False
