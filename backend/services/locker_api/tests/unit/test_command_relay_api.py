"""POST /commands и /commands/confirm для relay-типов: 201/202/403/409/422."""
from __future__ import annotations

from datetime import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg2.errors
from tornado.testing import AsyncHTTPTestCase

from app.handlers._relay_guard import parse_relay_parameters
from app.locker_data.db.models.command import CommandStatus, CommandType
from app.main import make_app
from app.services.auth_step_up_client import StepUpIssueResult, StepUpVerifyResult

LOCKER_ID = "550e8400-e29b-41d4-a716-446655440000"
URL = f"/api/v1/locker/{LOCKER_ID}/commands"
CONFIRM_URL = f"{URL}/confirm"
PROBLEM = "https://api.scuddocs.com/problems/"


def _locker(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": LOCKER_ID,
        "inventory_number": "СО №777", "location_address": "ул. Тестовая, 1",
        "location_lat": 55.75, "location_lon": 37.61,
        "auth_key": "k", "created_at": datetime.now(), "created_by": 1,
        "updated_at": datetime.now(), "updated_by": 1,
        "connectivity": "online", "door": "closed", "operational_status": "normal",
        "last_seen_at": None, "last_opened_at": None, "description": None, "metadata": {},
        "has_relays": True, "board_version": 2,
    }
    row.update(overrides)
    return row


def _relay(channel: int = 2, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "locker_id": LOCKER_ID, "channel": channel, "name": None, "purpose": None,
        "control_enabled": True, "is_protected": False, "is_active": True,
        "reported_state": 0, "changed_at": None,
        "created_at": datetime.now(), "updated_at": datetime.now(), "updated_by": None,
    }
    row.update(overrides)
    return row


def _command(command_type: CommandType, relay: int) -> dict[str, Any]:
    return {
        "id": "770e8400-e29b-41d4-a716-446655440002", "locker_id": LOCKER_ID,
        "type": command_type, "status": CommandStatus.PENDING,
        "parameters": {"relay": relay}, "created_at": datetime.now(), "created_by": 1,
        "sent_at": None, "delivered_at": None, "executed_at": None, "failed_at": None,
        "result": {}, "error_message": None, "retry_count": 0, "max_retries": 3,
    }


class _RelayApiBase(AsyncHTTPTestCase):
    def get_app(self) -> Any:
        self.db = MagicMock()
        self.db.fetch_one = AsyncMock(return_value=None)
        self.db.fetch_one_strict = AsyncMock()
        self.db.fetch_all = AsyncMock(return_value=[])
        self.db.execute = AsyncMock(return_value=0)
        queue = MagicMock()
        queue.is_connected.return_value = False
        queue.command_ttl_s = 60
        return make_app(self.db, queue)

    def post(self, url: str, body: dict[str, Any], role: str = "operator",
             require_2fa: bool = False) -> Any:
        headers = {
            "X-Remote-User": "1", "X-Remote-Role": role, "X-Remote-Lockers": "*",
            "Content-Type": "application/json",
        }
        if require_2fa:
            headers["X-Remote-Require-2FA"] = "true"
        return self.fetch(url, method="POST", headers=headers, body=json.dumps(body))

    def assert_problem(self, response: Any, status: int, code: str) -> None:
        self.assertEqual(response.code, status, response.body)
        self.assertEqual(json.loads(response.body)["type"], PROBLEM + code)


class TestRelayCommandCreate(_RelayApiBase):
    def test_operator_relay_on_201_with_normalized_parameters(self) -> None:
        self.db.fetch_one.side_effect = [_locker(), _relay(2)]
        self.db.fetch_one_strict.return_value = _command(CommandType.RELAY_ON, 2)
        response = self.post(URL, {"type": "relay_on", "parameters": {"relay": 2}})
        self.assertEqual(response.code, 201, response.body)
        self.assertEqual(json.loads(response.body)["type"], "relay_on")
        insert = next(
            c for c in self.db.fetch_one_strict.await_args_list
            if "INSERT INTO locker_commands" in c.args[0]
        )
        self.assertEqual(json.loads(insert.args[1][3]), {"relay": 2})

    def test_worker_forbidden_before_any_lookup(self) -> None:
        response = self.post(URL, {"type": "relay_off", "parameters": {"relay": 1}}, role="worker")
        self.assertEqual(response.code, 403)
        self.db.fetch_one.assert_not_awaited()

    def test_invalid_parameters_422(self) -> None:
        for parameters in (
            None, {}, {"relay": 0}, {"relay": 5}, {"relay": True}, {"relay": "1"},
            {"relay": 1.0}, {"relay": 1, "extra": 1}, [1],
        ):
            with self.subTest(parameters=parameters):
                self.db.fetch_one.side_effect = [_locker()]
                response = self.post(URL, {"type": "relay_on", "parameters": parameters})
                self.assert_problem(response, 422, "relay-invalid-parameters")
        self.db.fetch_one_strict.assert_not_awaited()

    def test_block_absent_409(self) -> None:
        for has_relays in (False, None):
            with self.subTest(has_relays=has_relays):
                self.db.fetch_one.side_effect = [_locker(has_relays=has_relays)]
                response = self.post(URL, {"type": "relay_on", "parameters": {"relay": 1}})
                self.assert_problem(response, 409, "relay-block-absent")

    def test_board_offline_409(self) -> None:
        self.db.fetch_one.side_effect = [_locker(connectivity="offline")]
        response = self.post(URL, {"type": "relay_on", "parameters": {"relay": 1}})
        self.assert_problem(response, 409, "board-offline")

    def test_channel_unavailable_409(self) -> None:
        for relay_row in (None, _relay(1, is_active=False), _relay(1, control_enabled=False)):
            with self.subTest(relay_row=relay_row):
                self.db.fetch_one.side_effect = [_locker(), relay_row]
                response = self.post(URL, {"type": "relay_restart", "parameters": {"relay": 1}})
                self.assert_problem(response, 409, "relay-channel-unavailable")

    def test_protected_channel_operator_403(self) -> None:
        self.db.fetch_one.side_effect = [_locker(), _relay(3, is_protected=True)]
        response = self.post(URL, {"type": "relay_off", "parameters": {"relay": 3}})
        self.assert_problem(response, 403, "relay-channel-protected")
        self.db.fetch_one_strict.assert_not_awaited()

    def test_protected_channel_administrator_201(self) -> None:
        self.db.fetch_one.side_effect = [_locker(), _relay(3, is_protected=True)]
        self.db.fetch_one_strict.return_value = _command(CommandType.RELAY_OFF, 3)
        response = self.post(
            URL, {"type": "relay_off", "parameters": {"relay": 3}}, role="administrator",
        )
        self.assertEqual(response.code, 201, response.body)

    def test_active_command_unique_violation_409(self) -> None:
        self.db.fetch_one.side_effect = [_locker(), _relay(2)]
        self.db.fetch_one_strict.side_effect = psycopg2.errors.UniqueViolation("dup")
        response = self.post(URL, {"type": "relay_on", "parameters": {"relay": 2}})
        self.assert_problem(response, 409, "relay-command-in-progress")

    def test_unique_violation_on_non_relay_command_not_mapped(self) -> None:
        self.db.fetch_one.side_effect = [_locker()]
        self.db.fetch_one_strict.side_effect = psycopg2.errors.UniqueViolation("dup")
        response = self.post(URL, {"type": "open", "parameters": {}}, role="administrator")
        self.assertEqual(response.code, 500)

    def test_2fa_challenge_issued_only_after_checks(self) -> None:
        mixin = MagicMock()
        mixin.maybe_issue_challenge = AsyncMock(return_value=StepUpIssueResult(
            challenge_id="cid", expires_at="2026-01-01T00:00:00Z", delivery_status="sent",
        ))
        self._app.settings["step_up_mixin"] = mixin

        self.db.fetch_one.side_effect = [_locker(), _relay(2, is_protected=True)]
        denied = self.post(URL, {"type": "relay_on", "parameters": {"relay": 2}}, require_2fa=True)
        self.assert_problem(denied, 403, "relay-channel-protected")
        mixin.maybe_issue_challenge.assert_not_awaited()

        self.db.fetch_one.side_effect = [_locker(), _relay(2)]
        response = self.post(URL, {"type": "relay_on", "parameters": {"relay": 2}}, require_2fa=True)
        self.assertEqual(response.code, 202, response.body)
        self.assertEqual(json.loads(response.body)["challenge_id"], "cid")
        kwargs = mixin.maybe_issue_challenge.await_args.kwargs
        self.assertEqual(kwargs["command_type"], "relay_on")
        self.assertEqual(kwargs["parameters"], {"relay": 2})


class TestRelayCommandConfirm(_RelayApiBase):
    def _verify_returns(self, command_type: str, parameters: Any) -> None:
        client = MagicMock()
        client.verify = AsyncMock(return_value=StepUpVerifyResult(
            scope_kind="locker_command",
            scope_payload={
                "locker_id": LOCKER_ID, "locker_inv": "СО №777",
                "command_type": command_type, "parameters": parameters, "max_retries": 3,
            },
        ))
        self._app.settings["auth_step_up_client"] = client

    def _confirm(self, role: str = "operator") -> Any:
        return self.post(CONFIRM_URL, {"challenge_id": "cid", "code": "123456"}, role=role)

    def test_confirm_enqueues_relay_command(self) -> None:
        self._verify_returns("relay_restart", {"relay": 4})
        self.db.fetch_one.side_effect = [_locker(), _relay(4)]
        self.db.fetch_one_strict.return_value = _command(CommandType.RELAY_RESTART, 4)
        response = self._confirm()
        self.assertEqual(response.code, 201, response.body)

    def test_confirm_rechecks_policy_changed_after_issue(self) -> None:
        self._verify_returns("relay_off", {"relay": 1})
        self.db.fetch_one.side_effect = [_locker(), _relay(1, control_enabled=False)]
        response = self._confirm()
        self.assert_problem(response, 409, "relay-channel-unavailable")
        self.db.fetch_one_strict.assert_not_awaited()

    def test_confirm_rechecks_protected_channel(self) -> None:
        self._verify_returns("relay_off", {"relay": 1})
        self.db.fetch_one.side_effect = [_locker(), _relay(1, is_protected=True)]
        self.assert_problem(self._confirm(), 403, "relay-channel-protected")

    def test_confirm_rechecks_board_offline(self) -> None:
        self._verify_returns("relay_on", {"relay": 1})
        self.db.fetch_one.side_effect = [_locker(connectivity="offline")]
        self.assert_problem(self._confirm(), 409, "board-offline")

    def test_confirm_rechecks_block_absent(self) -> None:
        self._verify_returns("relay_on", {"relay": 1})
        self.db.fetch_one.side_effect = [_locker(has_relays=False)]
        self.assert_problem(self._confirm(), 409, "relay-block-absent")

    def test_confirm_rejects_bad_parameters(self) -> None:
        self._verify_returns("relay_on", {"relay": 9})
        self.db.fetch_one.side_effect = [_locker()]
        self.assert_problem(self._confirm(), 422, "relay-invalid-parameters")

    def test_confirm_worker_forbidden(self) -> None:
        self._verify_returns("relay_on", {"relay": 1})
        self.assertEqual(self._confirm(role="worker").code, 403)

    def test_confirm_unique_violation_409(self) -> None:
        self._verify_returns("relay_on", {"relay": 2})
        self.db.fetch_one.side_effect = [_locker(), _relay(2)]
        self.db.fetch_one_strict.side_effect = psycopg2.errors.UniqueViolation("dup")
        self.assert_problem(self._confirm(), 409, "relay-command-in-progress")


def test_parse_relay_parameters_accepts_channels_1_to_4() -> None:
    for channel in (1, 2, 3, 4):
        params = parse_relay_parameters({"relay": channel})
        assert params is not None
        assert params.relay == channel


def test_parse_relay_parameters_rejects_garbage() -> None:
    for raw in (None, "1", 1, [], {}, {"relay": None}, {"relay": False}, {"relay": -1}):
        assert parse_relay_parameters(raw) is None
