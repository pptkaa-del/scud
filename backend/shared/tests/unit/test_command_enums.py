"""Тесты single-source CommandType/CommandStatus в shared.enums.

CommandType/CommandStatus — DB-контракт: значения и порядок членов должны
оставаться неизменными (PG enum locker_commands.type/.status, history
command_history.command_type/.status). Эти тесты фиксируют контракт и то,
что shared.access_control.commands строит матрицу из этих enum'ов.

Запускаются в shared_tests — без импорта app.* модулей сервисов.
"""
from __future__ import annotations

import unittest

from shared.access_control.commands import (
    COMMAND_PERMISSION_MATRIX,
    OTP_REQUIRED_COMMANDS,
    check_command_permission,
)
from shared.enums import CommandStatus, CommandType, UserRole


class TestCommandEnumContract(unittest.TestCase):
    def test_command_type_values(self) -> None:
        self.assertEqual(
            {m.name: m.value for m in CommandType},
            {
                "OPEN": "open", "CLOSE": "close", "REBOOT": "reboot", "CONFIG": "config",
                "RELAY_ON": "relay_on", "RELAY_OFF": "relay_off",
                "RELAY_RESTART": "relay_restart",
            },
        )

    def test_command_status_values(self) -> None:
        self.assertEqual(
            {m.name: m.value for m in CommandStatus},
            {
                "PENDING": "pending",
                "SENT": "sent",
                "DELIVERED": "delivered",
                "EXECUTED": "executed",
                "FAILED": "failed",
            },
        )

    def test_command_status_member_order(self) -> None:
        # Порядок должен совпадать с PG enum command_status (001_init.sql).
        self.assertEqual(
            list(CommandStatus),
            [
                CommandStatus.PENDING,
                CommandStatus.SENT,
                CommandStatus.DELIVERED,
                CommandStatus.EXECUTED,
                CommandStatus.FAILED,
            ],
        )


class TestCommandPermissionMatrix(unittest.TestCase):
    def test_worker_matrix(self) -> None:
        self.assertEqual(
            COMMAND_PERMISSION_MATRIX[UserRole.WORKER.value],
            frozenset({"open", "close"}),
        )

    def test_otp_required_commands(self) -> None:
        # `config` выведен из эксплуатации — больше не требует OTP.
        self.assertEqual(
            OTP_REQUIRED_COMMANDS,
            frozenset({"open", "reboot", "relay_on", "relay_off", "relay_restart"}),
        )

    def test_check_command_permission(self) -> None:
        self.assertIs(check_command_permission("worker", "open"), True)
        self.assertIs(check_command_permission("worker", "reboot"), False)
        # `config` выведен из эксплуатации — недоступен ни одной роли.
        self.assertIs(check_command_permission("operator", "config"), False)
        self.assertIs(check_command_permission("administrator", "config"), False)
        self.assertIs(check_command_permission("", "open"), False)
