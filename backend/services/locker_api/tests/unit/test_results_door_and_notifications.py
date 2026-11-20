"""Регрессия SIMP-10: MQTT relay command-result путь должен достигать паритета
с WS-путём по обновлению состояния двери и уведомлениям.

Успешный OPEN-rep обязан:
  - вызвать LockerRepository.update_door(locker_id, DoorState.OPEN, who_open_last=...)
  - создать постоянное уведомление об открытии (best-effort).
Успешный CLOSE-rep обязан:
  - вызвать LockerRepository.update_door(locker_id, DoorState.CLOSED)
  - отменить уведомления об открытии.

До фикса RelayCommandResults дёргал только status + history — дверь и
уведомления оставались несинхронизированными.
"""
import asyncio
from types import SimpleNamespace

from app.locker_data.db.models.command import CommandType
from app.locker_data.db.models.locker import DoorState
from app.mqtt.relay_protocol import RepResult
from app.mqtt.results import (
    _NOTIF_EVENT_OPENED,
    NOTIF_DISMISS_BY,
    RelayCommandResults,
)


class FakeCommands:
    def __init__(self, command):
        self._command = command
        self.executed_calls = []

    async def resolve_relay_cmd_id(self, locker_id, relay_cmd_id):
        return self._command.id

    async def update_status_to_executed(self, command_id, locker_id, result=None):
        self.executed_calls.append((command_id, locker_id))
        return self._command

    async def mark_device_rejected(self, command_id, locker_id, reason, rejected_reasons):
        self.rejected_calls = [*getattr(self, "rejected_calls", []),
                               (command_id, locker_id, reason, tuple(rejected_reasons))]
        return True

    async def update_status_to_failed(self, command_id, locker_id, error_message, result=None):
        return self._command


class FakeHistory:
    async def publish_executed(self, command_id, locker_id, command_type, **kwargs):
        pass

    async def publish_failed(self, command_id, locker_id, command_type, error_message, **kwargs):
        pass


class FakeLockerRepo:
    def __init__(self, inventory_number="INV-001"):
        self.door_calls = []
        self._locker = SimpleNamespace(inventory_number=inventory_number)

    async def update_door(self, locker_id, door, who_open_last=None):
        self.door_calls.append((locker_id, door, who_open_last))

    async def get_by_id(self, locker_id):
        return self._locker


class FakeNotificationClient:
    def __init__(self):
        self.created = []
        self.cancelled = []

    async def create_persistent(self, text, tags, roles=None, user_ids=None):
        self.created.append(tags)
        return "notif-1"

    async def cancel_locker_open_notifications(self, locker_id):
        self.cancelled.append(locker_id)
        return 1


def _make_results(command):
    commands = FakeCommands(command)
    locker_repo = FakeLockerRepo()
    notifier = FakeNotificationClient()
    results = RelayCommandResults(
        command_repo=commands,
        history_producer=FakeHistory(),
        locker_repo=locker_repo,
        notification_client=notifier,
    )
    return results, locker_repo, notifier


def test_open_success_updates_door_and_notifies():
    async def scenario():
        command = SimpleNamespace(
            id="cmd-uuid", locker_id="L1", type=CommandType.OPEN, created_by=42,
            parameters=None, sent_at=None, executed_at=None, failed_at=None,
        )
        results, locker_repo, notifier = _make_results(command)

        await results.handle_rep("L1", 7, RepResult.SUCCESS)

        assert locker_repo.door_calls == [("L1", DoorState.OPEN, 42)]
        assert len(notifier.created) == 1
        # Полный паритет тег-схемы с WS-путём (device_handler._notify_locker_opened):
        # notification_api матчит cancel по этой форме, поэтому фиксируем целиком.
        assert notifier.created[0] == {
            "event": _NOTIF_EVENT_OPENED,
            "locker_id": "L1",
            "opened_by_user_id": "42",
            "dismiss_by": NOTIF_DISMISS_BY,
        }
        assert notifier.cancelled == []
    asyncio.run(scenario())


def test_close_success_updates_door_and_cancels_notification():
    async def scenario():
        command = SimpleNamespace(
            id="cmd-uuid", locker_id="L1", type=CommandType.CLOSE, created_by=42,
            parameters=None, sent_at=None, executed_at=None, failed_at=None,
        )
        results, locker_repo, notifier = _make_results(command)

        await results.handle_rep("L1", 7, RepResult.SUCCESS)

        assert locker_repo.door_calls == [("L1", DoorState.CLOSED, None)]
        # Cancel обязан таргетить ровно тот locker_id, по которому WS-путь
        # снимает locker_opened-уведомление (event-матчинг живёт внутри клиента).
        assert notifier.cancelled == ["L1"]
        assert notifier.created == []
    asyncio.run(scenario())
