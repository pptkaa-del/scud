from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tornado.testing import AsyncHTTPTestCase

from app.locker_data.db.models.locker import Connectivity
from app.locker_data.db.models.relay import LockerRelay, RelayPendingCommand
from app.main import make_app
from shared.enums import CommandStatus, CommandType

LOCKER_ID = "550e8400-e29b-41d4-a716-446655440000"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
ALL_ACTIONS = ["relay_on", "relay_off", "relay_restart"]


def _headers(role, lockers=None):
    headers = {"X-Remote-User": "7", "X-Remote-Role": role}
    if lockers is not None:
        headers["X-Remote-Lockers"] = lockers
    return headers


def _locker(has_relays=True, board_version=2):
    return SimpleNamespace(
        id=LOCKER_ID, has_relays=has_relays, board_version=board_version,
        connectivity=Connectivity.ONLINE, last_seen_at=NOW,
    )


def _relay(channel, **overrides):
    values = {
        "locker_id": LOCKER_ID, "channel": channel, "name": None, "purpose": None,
        "control_enabled": True, "is_protected": False, "is_active": True,
        "reported_state": 1, "changed_at": NOW, "created_at": NOW, "updated_at": NOW,
        "updated_by": None,
    }
    values.update(overrides)
    return LockerRelay(**values)


class RelayApiCase(AsyncHTTPTestCase):
    def get_app(self):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value=None)
        db.fetch_all = AsyncMock(return_value=[])
        db.execute = AsyncMock()
        return make_app(db, MagicMock())

    def setUp(self):
        super().setUp()
        self.lockers = MagicMock()
        self.lockers.get_by_id = AsyncMock(return_value=_locker())
        self.relays = MagicMock()
        self.relays.list_for_locker = AsyncMock(return_value=[])
        self.relays.pending_commands = AsyncMock(return_value={})
        self.relays.update_settings = AsyncMock(return_value=None)
        patchers = [
            patch("app.handlers.relay.LockerRepository", return_value=self.lockers),
            patch("app.handlers.relay.RelayRepository", return_value=self.relays),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _get(self, headers):
        return self.fetch(f"/api/v1/locker/{LOCKER_ID}/relays", headers=headers)

    def _patch(self, channel, body, role="administrator"):
        return self.fetch(
            f"/api/v1/locker/{LOCKER_ID}/relays/{channel}", method="PATCH",
            headers=_headers(role), body=json.dumps(body),
        )


class TestGetRelays(RelayApiCase):
    def test_operator_sees_channels_with_actions_and_pending(self):
        self.relays.list_for_locker.return_value = [
            _relay(1), _relay(2, is_protected=True), _relay(3, control_enabled=False),
        ]
        self.relays.pending_commands.return_value = {
            1: RelayPendingCommand(id="c1", type=CommandType.RELAY_OFF,
                                   status=CommandStatus.SENT, channel=1),
        }
        resp = self._get(_headers("operator"))
        assert resp.code == 200
        body = json.loads(resp.body)
        assert body["locker_id"] == LOCKER_ID
        assert body["has_relays"] is True
        assert body["board_version"] == 2
        assert body["connectivity"] == "online"
        assert body["reported_at"] == NOW.isoformat()
        items = body["items"]
        assert [i["channel"] for i in items] == [1, 2, 3]
        assert items[0]["allowed_actions"] == ALL_ACTIONS
        assert items[0]["pending_command"] == {"id": "c1", "type": "relay_off", "status": "sent"}
        assert items[1]["allowed_actions"] == []
        assert items[2]["allowed_actions"] == []
        assert items[1]["pending_command"] is None
        assert items[0]["reported_state"] == 1

    def test_administrator_may_control_protected_channel(self):
        self.relays.list_for_locker.return_value = [_relay(2, is_protected=True)]
        body = json.loads(self._get(_headers("administrator")).body)
        assert body["items"][0]["allowed_actions"] == ALL_ACTIONS

    def test_worker_with_grant_reads_without_actions(self):
        self.relays.list_for_locker.return_value = [_relay(1)]
        resp = self._get(_headers("worker", lockers=LOCKER_ID))
        assert resp.code == 200
        assert json.loads(resp.body)["items"][0]["allowed_actions"] == []

    def test_worker_without_grant_is_forbidden(self):
        resp = self._get(_headers("worker", lockers=""))
        assert resp.code == 403
        self.lockers.get_by_id.assert_not_awaited()

    def test_no_actions_when_block_absent(self):
        self.lockers.get_by_id.return_value = _locker(has_relays=False)
        self.relays.list_for_locker.return_value = [_relay(1)]
        body = json.loads(self._get(_headers("administrator")).body)
        assert body["has_relays"] is False
        assert body["items"][0]["allowed_actions"] == []

    def test_empty_items_without_rows(self):
        self.lockers.get_by_id.return_value = _locker(has_relays=None, board_version=None)
        body = json.loads(self._get(_headers("operator")).body)
        assert body["items"] == []
        assert body["has_relays"] is None
        self.relays.pending_commands.assert_not_awaited()

    def test_unknown_locker_404(self):
        self.lockers.get_by_id.return_value = None
        assert self._get(_headers("operator")).code == 404

    def test_bad_uuid_404(self):
        resp = self.fetch("/api/v1/locker/not-a-uuid/relays", headers=_headers("operator"))
        assert resp.code == 404


class TestPatchRelay(RelayApiCase):
    def test_admin_updates_channel(self):
        self.relays.update_settings.return_value = _relay(2, name="Насос", is_protected=True)
        resp = self._patch(2, {"name": "Насос", "is_protected": True})
        assert resp.code == 200
        body = json.loads(resp.body)
        assert body["channel"] == 2
        assert body["name"] == "Насос"
        assert body["allowed_actions"] == ALL_ACTIONS
        self.relays.update_settings.assert_awaited_once_with(
            LOCKER_ID, 2, {"name": "Насос", "is_protected": True}, 7,
        )

    def test_null_name_clears_it(self):
        self.relays.update_settings.return_value = _relay(1)
        assert self._patch(1, {"name": None}).code == 200
        self.relays.update_settings.assert_awaited_once_with(LOCKER_ID, 1, {"name": None}, 7)

    def test_operator_forbidden(self):
        assert self._patch(1, {"is_active": False}, role="operator").code == 403
        self.relays.update_settings.assert_not_awaited()

    def test_channel_outside_range_is_not_routed(self):
        assert self._patch(5, {"is_active": False}).code == 404

    def test_validation_errors_are_422(self):
        for body in (
            {},
            {"unknown": 1},
            {"is_active": None},
            {"control_enabled": "yes"},
            {"is_protected": 1},
            {"name": 5},
            {"name": "x" * 101},
        ):
            resp = self._patch(1, body)
            assert resp.code == 422, body
        self.relays.update_settings.assert_not_awaited()

    def test_missing_channel_row_404(self):
        self.relays.update_settings.return_value = None
        assert self._patch(1, {"is_active": False}).code == 404

    def test_unknown_locker_404(self):
        self.lockers.get_by_id.return_value = None
        assert self._patch(1, {"is_active": False}).code == 404
