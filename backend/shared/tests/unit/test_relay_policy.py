"""authorize_relay_command: полная матрица роль × тип команды × политика канала."""
from __future__ import annotations

from itertools import product
import unittest

from shared.access_control import (
    RELAY_COMMAND_TYPES,
    LockerRelayPolicy,
    RelayDecision,
    RelayProblem,
    allowed_relay_actions,
    authorize_relay_command,
)
from shared.enums import CommandType, UserRole

ALL_ROLES: tuple[UserRole, ...] = tuple(UserRole)
ALL_COMMAND_TYPES: tuple[CommandType, ...] = tuple(CommandType)
RELAY_TYPES = (CommandType.RELAY_ON, CommandType.RELAY_OFF, CommandType.RELAY_RESTART)
OPEN_CHANNEL = LockerRelayPolicy(control_enabled=True, is_protected=False, is_active=True)
PROTECTED_CHANNEL = LockerRelayPolicy(control_enabled=True, is_protected=True, is_active=True)
POLICIES: tuple[LockerRelayPolicy | None, ...] = (
    None,
    *(
        LockerRelayPolicy(control_enabled=c, is_protected=p, is_active=a)
        for c, p, a in product((True, False), repeat=3)
    ),
)


def _expected(
    role: UserRole,
    command_type: CommandType,
    has_relays: bool | None,
    relay: LockerRelayPolicy | None,
) -> RelayProblem | None:
    if command_type not in RELAY_TYPES or role == UserRole.WORKER:
        return RelayProblem.COMMAND_FORBIDDEN
    if has_relays is not True:
        return RelayProblem.BLOCK_ABSENT
    if relay is None or not relay.is_active or not relay.control_enabled:
        return RelayProblem.CHANNEL_UNAVAILABLE
    if relay.is_protected and role != UserRole.ADMINISTRATOR:
        return RelayProblem.CHANNEL_PROTECTED
    return None


class TestAuthorizeRelayCommandMatrix(unittest.TestCase):
    def test_full_matrix(self) -> None:
        cases = 0
        for role, ctype, has_relays, relay in product(
            ALL_ROLES, ALL_COMMAND_TYPES, (True, False, None), POLICIES,
        ):
            with self.subTest(role=role, ctype=ctype, has_relays=has_relays, relay=relay):
                decision = authorize_relay_command(role, ctype, has_relays=has_relays, relay=relay)
                expected = _expected(role, ctype, has_relays, relay)
                self.assertEqual(decision.denial, expected)
                self.assertIs(decision.allowed, expected is None)
            cases += 1
        self.assertEqual(cases, len(ALL_ROLES) * len(ALL_COMMAND_TYPES) * 3 * len(POLICIES))

    def test_relay_types_constant(self) -> None:
        self.assertEqual(RELAY_COMMAND_TYPES, frozenset(RELAY_TYPES))


class TestAuthorizeRelayCommandCases(unittest.TestCase):
    def test_operator_open_channel_allowed(self) -> None:
        for ctype in RELAY_TYPES:
            decision = authorize_relay_command(
                UserRole.OPERATOR, ctype, has_relays=True, relay=OPEN_CHANNEL,
            )
            self.assertTrue(decision.allowed)
            self.assertIsNone(decision.http_status)

    def test_administrator_protected_channel_allowed(self) -> None:
        decision = authorize_relay_command(
            UserRole.ADMINISTRATOR, CommandType.RELAY_OFF, has_relays=True, relay=PROTECTED_CHANNEL,
        )
        self.assertTrue(decision.allowed)

    def test_operator_protected_channel_403(self) -> None:
        decision = authorize_relay_command(
            UserRole.OPERATOR, CommandType.RELAY_OFF, has_relays=True, relay=PROTECTED_CHANNEL,
        )
        self.assertEqual(decision.denial, RelayProblem.CHANNEL_PROTECTED)
        self.assertEqual(decision.http_status, 403)

    def test_worker_forbidden_even_on_open_channel(self) -> None:
        decision = authorize_relay_command(
            UserRole.WORKER, CommandType.RELAY_ON, has_relays=True, relay=OPEN_CHANNEL,
        )
        self.assertEqual(decision.denial, RelayProblem.COMMAND_FORBIDDEN)
        self.assertEqual(decision.http_status, 403)

    def test_role_checked_before_block_presence(self) -> None:
        decision = authorize_relay_command(
            UserRole.WORKER, CommandType.RELAY_ON, has_relays=False, relay=None,
        )
        self.assertEqual(decision.denial, RelayProblem.COMMAND_FORBIDDEN)

    def test_unknown_block_is_absent(self) -> None:
        for has_relays in (False, None):
            decision = authorize_relay_command(
                UserRole.ADMINISTRATOR, CommandType.RELAY_ON, has_relays=has_relays, relay=OPEN_CHANNEL,
            )
            self.assertEqual(decision.denial, RelayProblem.BLOCK_ABSENT)
            self.assertEqual(decision.http_status, 409)

    def test_missing_inactive_or_disabled_channel_unavailable(self) -> None:
        for relay in (
            None,
            LockerRelayPolicy(control_enabled=True, is_protected=False, is_active=False),
            LockerRelayPolicy(control_enabled=False, is_protected=False, is_active=True),
        ):
            decision = authorize_relay_command(
                UserRole.ADMINISTRATOR, CommandType.RELAY_RESTART, has_relays=True, relay=relay,
            )
            self.assertEqual(decision.denial, RelayProblem.CHANNEL_UNAVAILABLE)
            self.assertEqual(decision.http_status, 409)

    def test_non_relay_command_forbidden(self) -> None:
        for ctype in (CommandType.OPEN, CommandType.CLOSE, CommandType.REBOOT, CommandType.CONFIG):
            decision = authorize_relay_command(
                UserRole.ADMINISTRATOR, ctype, has_relays=True, relay=OPEN_CHANNEL,
            )
            self.assertEqual(decision.denial, RelayProblem.COMMAND_FORBIDDEN)

    def test_default_decision_is_allowed(self) -> None:
        self.assertTrue(RelayDecision().allowed)


class TestAllowedRelayActions(unittest.TestCase):
    def test_operator_open_channel_gets_all_in_order(self) -> None:
        self.assertEqual(
            allowed_relay_actions(UserRole.OPERATOR, has_relays=True, relay=OPEN_CHANNEL),
            list(RELAY_TYPES),
        )

    def test_operator_protected_channel_gets_nothing(self) -> None:
        self.assertEqual(
            allowed_relay_actions(UserRole.OPERATOR, has_relays=True, relay=PROTECTED_CHANNEL), [],
        )

    def test_administrator_protected_channel_gets_all(self) -> None:
        self.assertEqual(
            allowed_relay_actions(UserRole.ADMINISTRATOR, has_relays=True, relay=PROTECTED_CHANNEL),
            list(RELAY_TYPES),
        )

    def test_worker_gets_nothing(self) -> None:
        self.assertEqual(
            allowed_relay_actions(UserRole.WORKER, has_relays=True, relay=OPEN_CHANNEL), [],
        )

    def test_absent_block_gets_nothing(self) -> None:
        self.assertEqual(
            allowed_relay_actions(UserRole.ADMINISTRATOR, has_relays=None, relay=OPEN_CHANNEL), [],
        )


if __name__ == "__main__":
    unittest.main()
