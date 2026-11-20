"""CommandType/CommandStatus в модели history — это re-export shared.enums.

Фиксирует, что command_history.CommandType/CommandStatus — те же объекты,
что в shared.enums (единый источник, DB-контракт не дублируется).
"""
from __future__ import annotations

import unittest

from dacite import from_dict

from app.handlers.command_history import _DTO_CONFIG
from app.handlers.dto.command_history_dto import CommandHistoryCreateDTO
from app.history_data.db.models.command_history import CommandStatus, CommandType
import shared.enums as se


class TestCommandEnumSource(unittest.TestCase):
    def test_command_enums_are_shared(self) -> None:
        self.assertIs(CommandType, se.CommandType)
        self.assertIs(CommandStatus, se.CommandStatus)


class TestRelayCommandTypesAccepted(unittest.TestCase):
    def test_create_dto_casts_relay_types(self) -> None:
        for value in ("relay_on", "relay_off", "relay_restart"):
            dto = from_dict(CommandHistoryCreateDTO, {
                "command_id": "c", "locker_id": "l",
                "command_type": value, "status": "pending",
                "parameters": {"relay": 1},
            }, config=_DTO_CONFIG)
            self.assertIs(dto.command_type, CommandType(value))
