"""Unit-тесты RelayCommandResults.handle_rep.

Маппинг relay rep-результата на locker-command lifecycle:
  SUCCESS       → update_status_to_executed + publish_executed
  FAILURE       → update_status_to_failed(reason="failed")
  NOT_SUPPORTED → update_status_to_failed(reason="not_supported")
  unknown relay_cmd_id → warning + no-op (никаких repo/history вызовов).

Все зависимости — простые фейки (command_repo / history_producer).
"""
import asyncio
from datetime import UTC, datetime, timedelta
import logging
from types import SimpleNamespace

from app.locker_data.db.models.command import CommandStatus, CommandType
from app.mqtt.relay_protocol import RepResult
from app.mqtt.results import _NOTIF_EVENT_LATE, RelayCommandResults
from app.services.command_expiry import COMMAND_TTL_FAILURE_REASON
from shared.enums import RelayMessageOutcome


class FakeCommands:
    def __init__(self, resolve_to="cmd-uuid", command=None, *, cas_hits=True, current=None):
        self._resolve_to = resolve_to
        self._command = command or SimpleNamespace(
            id="cmd-uuid", locker_id="L1", type="open",
            created_by=None, parameters=None,
            sent_at=None, executed_at=None, failed_at=None,
        )
        self._cas_hits = cas_hits
        self._current = current
        self.executed_calls = []
        self.failed_calls = []

    async def get_by_id(self, command_id):
        return self._current

    async def resolve_relay_cmd_id(self, locker_id, relay_cmd_id):
        return self._resolve_to

    async def update_status_to_executed(self, command_id, locker_id, result=None):
        self.executed_calls.append((command_id, locker_id, result))
        return self._command if self._cas_hits else None

    async def mark_device_rejected(self, command_id, locker_id, reason, rejected_reasons):
        self.rejected_calls = [*getattr(self, "rejected_calls", []),
                               (command_id, locker_id, reason, tuple(rejected_reasons))]
        return True

    async def update_status_to_failed(self, command_id, locker_id, error_message, result=None):
        self.failed_calls.append((command_id, locker_id, error_message, result))
        return self._command if self._cas_hits else None


class FakeHistory:
    def __init__(self):
        self.executed = []
        self.failed = []

    async def publish_executed(self, command_id, locker_id, command_type, **kwargs):
        entry = {"command_id": command_id, "locker_id": locker_id, "command_type": command_type}
        entry.update(kwargs)
        self.executed.append(entry)

    async def publish_failed(self, command_id, locker_id, command_type, error_message, **kwargs):
        entry = {
            "command_id": command_id,
            "locker_id": locker_id,
            "command_type": command_type,
            "error_message": error_message,
        }
        entry.update(kwargs)
        self.failed.append(entry)


class FakeLockerRepo:
    def __init__(self, inventory_number: str | None = None):
        self._inventory_number = inventory_number

    async def get_by_id(self, locker_id: str):
        if self._inventory_number is not None:
            return SimpleNamespace(inventory_number=self._inventory_number)
        return None


def _make_command(**overrides):
    ns = SimpleNamespace(
        id="cmd-uuid", locker_id="L1", type="open",
        created_by=None, parameters=None,
        sent_at=None, executed_at=None, failed_at=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def test_success_marks_executed_and_publishes():
    async def scenario():
        commands, history = FakeCommands(), FakeHistory()
        r = RelayCommandResults(command_repo=commands, history_producer=history)
        assert await r.handle_rep("L1", 7, RepResult.SUCCESS) == RelayMessageOutcome.PROCESSED
        # Команда переводится в executed С непустым result {success, message}
        # (Fix #3: раньше в колонку result писался {}).
        assert commands.executed_calls == [
            ("cmd-uuid", "L1", {"success": True, "message": "Command executed successfully"})
        ]
        assert commands.failed_calls == []
        assert len(history.executed) == 1
        assert history.executed[0]["command_id"] == "cmd-uuid"
        assert history.executed[0]["locker_id"] == "L1"
        assert history.executed[0]["command_type"] == "open"
        assert history.failed == []
    asyncio.run(scenario())


def test_failure_marks_failed_with_failed_reason():
    async def scenario():
        commands, history = FakeCommands(), FakeHistory()
        r = RelayCommandResults(command_repo=commands, history_producer=history)
        assert await r.handle_rep("L1", 7, RepResult.FAILURE) == RelayMessageOutcome.PROCESSED
        assert commands.failed_calls == [
            ("cmd-uuid", "L1", "failed", {"success": False, "message": "failed"})
        ]
        assert commands.executed_calls == []
        assert len(history.failed) == 1
        assert history.failed[0]["command_id"] == "cmd-uuid"
        assert history.failed[0]["locker_id"] == "L1"
        assert history.failed[0]["command_type"] == "open"
        assert history.failed[0]["error_message"] == "failed"
        assert history.executed == []
    asyncio.run(scenario())


def test_not_supported_marks_failed_with_not_supported_reason():
    async def scenario():
        commands, history = FakeCommands(), FakeHistory()
        r = RelayCommandResults(command_repo=commands, history_producer=history)
        await r.handle_rep("L1", 7, RepResult.NOT_SUPPORTED)
        assert commands.failed_calls == [
            ("cmd-uuid", "L1", "not_supported",
             {"success": False, "message": "not_supported"})
        ]
        assert len(history.failed) == 1
        assert history.failed[0]["error_message"] == "not_supported"
    asyncio.run(scenario())


def test_unknown_relay_cmd_id_is_noop():
    async def scenario():
        commands = FakeCommands(resolve_to=None)
        history = FakeHistory()
        r = RelayCommandResults(command_repo=commands, history_producer=history)
        assert await r.handle_rep("L1", 999, RepResult.SUCCESS) == RelayMessageOutcome.PROCESSED
        # Никаких status-переходов и history-событий.
        assert commands.executed_calls == []
        assert commands.failed_calls == []
        assert history.executed == []
        assert history.failed == []
    asyncio.run(scenario())


def test_executed_history_includes_result_parameters_created_by_inventory():
    async def scenario():
        cmd = _make_command(created_by=7, parameters={"relay": 1})
        commands = FakeCommands(command=cmd)
        history = FakeHistory()
        locker_repo = FakeLockerRepo(inventory_number="INV-9")
        r = RelayCommandResults(
            command_repo=commands,
            history_producer=history,
            locker_repo=locker_repo,
        )
        await r.handle_rep("L1", 7, RepResult.SUCCESS)
        assert len(history.executed) == 1
        call = history.executed[0]
        assert call["parameters"] == {"relay": 1}
        assert call["created_by"] == 7
        assert call["locker_inventory_number"] == "INV-9"
        assert call["result"] == {"rep_result": "SUCCESS"}
    asyncio.run(scenario())


def test_failed_history_includes_parameters_created_by_inventory():
    async def scenario():
        cmd = _make_command(created_by=7, parameters={"relay": 1})
        commands = FakeCommands(command=cmd)
        history = FakeHistory()
        locker_repo = FakeLockerRepo(inventory_number="INV-9")
        r = RelayCommandResults(
            command_repo=commands,
            history_producer=history,
            locker_repo=locker_repo,
        )
        await r.handle_rep("L1", 7, RepResult.FAILURE)
        assert len(history.failed) == 1
        call = history.failed[0]
        assert call["parameters"] == {"relay": 1}
        assert call["created_by"] == 7
        assert call["locker_inventory_number"] == "INV-9"
    asyncio.run(scenario())


# --- терминальная команда: stale rep / late execution -----------------------

_RECEIVED_AT = datetime(2026, 10, 4, 12, 0, 30, tzinfo=UTC)


class FakeLateProducer:
    def __init__(self):
        self.calls = []

    async def publish(self, locker_id, sensor_id, violations, **kwargs):
        self.calls.append({"locker_id": locker_id, "sensor_id": sensor_id,
                           "violations": violations, **kwargs})
        return 1


class FakeDoorLockerRepo:
    def __init__(self):
        self.door_calls = []

    async def get_by_id(self, locker_id):
        return SimpleNamespace(inventory_number="СО №777", location_address="ул. Тестовая, 1")

    async def update_door(self, locker_id, door, who_open_last=None):
        self.door_calls.append((locker_id, door))


class FakeNotifier:
    def __init__(self, fail=False):
        self.created = []
        self.cancelled = []
        self._fail = fail

    async def create_persistent(self, text, tags, roles=None, user_ids=None):
        if self._fail:
            raise RuntimeError("notification_api down")
        self.created.append({"text": text, "tags": tags, "roles": roles})
        return "n1"

    async def cancel_locker_open_notifications(self, locker_id):
        self.cancelled.append(locker_id)
        return 0


def _terminal(status, **kw):
    base = SimpleNamespace(
        id="cmd-uuid", locker_id="L1", type=CommandType.OPEN, status=status,
        created_by=42, parameters={}, sent_at=None,
        created_at=_RECEIVED_AT - timedelta(seconds=700),
        executed_at=None, failed_at=None, error_message=None,
    )
    for key, value in kw.items():
        setattr(base, key, value)
    return base


def _stale_setup(current, notifier=None):
    commands = FakeCommands(cas_hits=False, current=current)
    history = FakeHistory()
    late = FakeLateProducer()
    lockers = FakeDoorLockerRepo()
    notifier = notifier if notifier is not None else FakeNotifier()
    r = RelayCommandResults(
        command_repo=commands, history_producer=history,
        locker_repo=lockers, notification_client=notifier,
        command_ttl_s=60, late_execution_producer=late,
    )
    return r, history, late, lockers, notifier


def test_duplicate_success_on_executed_within_ttl_is_silent_stale(caplog):
    current = _terminal(CommandStatus.EXECUTED, executed_at=_RECEIVED_AT - timedelta(seconds=5))
    r, history, late, lockers, notifier = _stale_setup(current)
    with caplog.at_level(logging.INFO, logger="app.mqtt.results"):
        outcome = asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert outcome == RelayMessageOutcome.STALE_REP
    assert history.executed == []
    assert notifier.created == []
    assert lockers.door_calls == []
    assert late.calls == []
    assert any("stale rep" in rec.getMessage() and rec.levelno == logging.INFO
               for rec in caplog.records)


def test_late_success_after_failed_keeps_failed_and_emits_event():
    current = _terminal(CommandStatus.FAILED, error_message=COMMAND_TTL_FAILURE_REASON,
                        failed_at=_RECEIVED_AT - timedelta(seconds=640))
    r, history, late, lockers, notifier = _stale_setup(current)
    outcome = asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert outcome == RelayMessageOutcome.STALE_REP
    assert history.executed == []
    assert lockers.door_calls == []
    assert len(late.calls) == 1
    call = late.calls[0]
    assert call["locker_id"] == "L1"
    assert call["sensor_id"] is None
    description = call["violations"][0]
    assert "открыть" in description
    assert "ОШИБКА" in description
    assert COMMAND_TTL_FAILURE_REASON in description
    meta = call["extra_metadata"]
    assert meta["late_kind"] == "after_failure"
    assert meta["delay_s"] == 700
    assert meta["relay_cmd_id"] == 7
    assert meta["created_by"] == 42
    assert meta["rep_received_at"] == _RECEIVED_AT.isoformat()


def test_replay_success_long_after_executed_emits_replay_event():
    current = _terminal(CommandStatus.EXECUTED, executed_at=_RECEIVED_AT - timedelta(days=6))
    r, _history, late, _lockers, _notifier = _stale_setup(current)
    outcome = asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert outcome == RelayMessageOutcome.STALE_REP
    assert len(late.calls) == 1
    assert late.calls[0]["extra_metadata"]["late_kind"] == "replay"
    assert late.calls[0]["extra_metadata"]["delay_s"] == 6 * 86400


def test_replay_of_retained_close_long_after_executed_is_silent_stale():
    current = _terminal(CommandStatus.EXECUTED, type=CommandType.CLOSE,
                        executed_at=_RECEIVED_AT - timedelta(hours=4))
    r, history, late, lockers, notifier = _stale_setup(current)
    outcome = asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert outcome == RelayMessageOutcome.STALE_REP
    assert late.calls == []
    assert notifier.created == []
    assert history.executed == []
    assert lockers.door_calls == []


def test_late_close_after_failed_still_emits_event_and_notifies():
    current = _terminal(CommandStatus.FAILED, type=CommandType.CLOSE,
                        error_message=COMMAND_TTL_FAILURE_REASON)
    r, _history, late, _lockers, notifier = _stale_setup(current)
    asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert len(late.calls) == 1
    assert late.calls[0]["extra_metadata"]["late_kind"] == "after_failure"
    assert len(notifier.created) == 1


def test_late_event_uid_deterministic_for_duplicates():
    current = _terminal(CommandStatus.FAILED, error_message=COMMAND_TTL_FAILURE_REASON)
    r, _history, late, _lockers, _notifier = _stale_setup(current)
    bucket_start = datetime(2026, 10, 4, 12, 1, 0, tzinfo=UTC)
    for at in (bucket_start, bucket_start + timedelta(seconds=59),
               bucket_start + timedelta(seconds=60)):
        asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=at))
    uids = [c["event_uid"] for c in late.calls]
    assert uids[0] == uids[1]
    assert uids[2] != uids[0]


def test_late_failure_rep_on_terminal_is_silent():
    current = _terminal(CommandStatus.EXECUTED, executed_at=_RECEIVED_AT - timedelta(days=1))
    r, history, late, _lockers, notifier = _stale_setup(current)
    outcome = asyncio.run(r.handle_rep("L1", 7, RepResult.FAILURE, received_at=_RECEIVED_AT))
    assert outcome == RelayMessageOutcome.STALE_REP
    assert history.failed == []
    assert late.calls == []
    assert notifier.created == []


def test_cas_miss_unknown_keeps_warning_processed(caplog):
    r, _history, late, _lockers, _notifier = _stale_setup(None)
    with caplog.at_level(logging.WARNING, logger="app.mqtt.results"):
        outcome = asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert outcome == RelayMessageOutcome.PROCESSED
    assert late.calls == []
    assert any("command not found" in rec.getMessage() and rec.levelno == logging.WARNING
               for rec in caplog.records)


def test_cas_miss_foreign_locker_keeps_warning_processed():
    current = _terminal(CommandStatus.FAILED, locker_id="L2")
    r, _history, late, _lockers, notifier = _stale_setup(current)
    outcome = asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert outcome == RelayMessageOutcome.PROCESSED
    assert late.calls == []
    assert notifier.created == []


def test_accepted_rep_logs_info_returns_processed(caplog):
    commands, history = FakeCommands(), FakeHistory()
    r = RelayCommandResults(command_repo=commands, history_producer=history)
    with caplog.at_level(logging.INFO, logger="app.mqtt.results"):
        outcome = asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS))
    assert outcome == RelayMessageOutcome.PROCESSED
    assert any("rep accepted" in rec.getMessage() for rec in caplog.records)


# --- уведомление операторам о позднем исполнении ------------------------------

def test_late_execution_notifies_operators_once_per_event():
    current = _terminal(CommandStatus.FAILED, error_message=COMMAND_TTL_FAILURE_REASON)
    r, _history, late, _lockers, notifier = _stale_setup(current)
    asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS,
                             received_at=_RECEIVED_AT + timedelta(seconds=1)))
    assert len(late.calls) == 2
    assert len(notifier.created) == 1
    n = notifier.created[0]
    assert n["text"] == (
        "Шкаф СО №777 (ул. Тестовая, 1) выполнил команду «открыть» с опозданием"
        " — дверь могла открыться без оператора."
    )
    assert n["roles"] == ["operator", "administrator"]
    assert n["tags"]["event"] == _NOTIF_EVENT_LATE
    assert n["tags"]["locker_id"] == "L1"
    assert n["tags"]["command_id"] == "cmd-uuid"
    assert n["tags"]["late_kind"] == "after_failure"
    assert n["tags"]["event_uid"] == late.calls[0]["event_uid"]


def test_concurrent_duplicate_reps_notify_once():
    current = _terminal(CommandStatus.FAILED, error_message=COMMAND_TTL_FAILURE_REASON)
    r, _history, _late, _lockers, notifier = _stale_setup(current)

    async def both():
        await asyncio.gather(
            r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT),
            r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT),
        )

    asyncio.run(both())
    assert len(notifier.created) == 1


def test_late_replay_in_new_window_notifies_again():
    current = _terminal(CommandStatus.EXECUTED, executed_at=_RECEIVED_AT - timedelta(days=1))
    r, _history, _late, _lockers, notifier = _stale_setup(current)
    asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS,
                             received_at=_RECEIVED_AT + timedelta(minutes=10)))
    assert len(notifier.created) == 2
    assert notifier.created[0]["tags"]["late_kind"] == "replay"


def test_late_notification_failure_never_raises_and_allows_retry():
    current = _terminal(CommandStatus.FAILED, error_message=COMMAND_TTL_FAILURE_REASON)
    notifier = FakeNotifier(fail=True)
    r, _history, late, _lockers, _ = _stale_setup(current, notifier=notifier)
    outcome = asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert outcome == RelayMessageOutcome.STALE_REP
    assert len(late.calls) == 1
    notifier._fail = False
    asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert len(notifier.created) == 1


def test_late_reboot_notification_text():
    current = _terminal(CommandStatus.FAILED, type=CommandType.REBOOT,
                        error_message=COMMAND_TTL_FAILURE_REASON)
    r, _history, _late, _lockers, notifier = _stale_setup(current)
    asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert notifier.created[0]["text"].endswith(
        "выполнил команду «перезагрузить» с опозданием — шкаф мог перезагрузиться без оператора."
    )


def test_stale_rep_without_notifier_still_emits_event():
    current = _terminal(CommandStatus.FAILED, error_message=COMMAND_TTL_FAILURE_REASON)
    late = FakeLateProducer()
    r = RelayCommandResults(
        command_repo=FakeCommands(cas_hits=False, current=current),
        history_producer=FakeHistory(), late_execution_producer=late,
    )
    asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert len(late.calls) == 1


def test_late_notification_locker_lookup_error_never_raises():
    current = _terminal(CommandStatus.FAILED, error_message=COMMAND_TTL_FAILURE_REASON)
    r, _history, late, lockers, notifier = _stale_setup(current)
    calls = []
    original = lockers.get_by_id

    async def flaky(locker_id):
        calls.append(locker_id)
        if len(calls) > 1:
            raise RuntimeError("db down")
        return await original(locker_id)

    lockers.get_by_id = flaky
    outcome = asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert outcome == RelayMessageOutcome.STALE_REP
    assert len(late.calls) == 1
    assert notifier.created == []
    assert len(calls) == 2


def test_late_rejection_marks_ttl_failed_command_as_rejected():
    """Плата отвергла команду, когда та уже завершилась по TTL: отказ фиксируется,
    иначе отвергнутая OPEN считалась бы действующей командой замка."""
    for result, reason in ((RepResult.FAILURE, "failed"),
                           (RepResult.NOT_SUPPORTED, "not_supported")):
        current = _terminal(CommandStatus.FAILED, error_message=COMMAND_TTL_FAILURE_REASON)
        r, _, late, _, _ = _stale_setup(current)
        commands = r._commands
        asyncio.run(r.handle_rep("L1", 7, result, received_at=_RECEIVED_AT))
        assert commands.rejected_calls == [
            ("cmd-uuid", "L1", reason, ("failed", "not_supported"))]
        assert late.calls == []


def test_stale_rejection_of_executed_or_success_is_not_marked():
    executed = _terminal(CommandStatus.EXECUTED, executed_at=_RECEIVED_AT - timedelta(seconds=5))
    r, *_ = _stale_setup(executed)
    asyncio.run(r.handle_rep("L1", 7, RepResult.FAILURE, received_at=_RECEIVED_AT))
    assert not hasattr(r._commands, "rejected_calls")
    failed = _terminal(CommandStatus.FAILED, error_message=COMMAND_TTL_FAILURE_REASON)
    r, *_ = _stale_setup(failed)
    asyncio.run(r.handle_rep("L1", 7, RepResult.SUCCESS, received_at=_RECEIVED_AT))
    assert not hasattr(r._commands, "rejected_calls")
