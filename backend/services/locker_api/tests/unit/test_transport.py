import asyncio

from app.mqtt.relay_protocol import topic_com
from app.mqtt.transport import MqttTransport


class FakeMqtt:
    def __init__(self, connected=True):
        self.published = []
        self.is_connected = connected

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


def test_mqtt_transport_publishes_retained_com():
    async def scenario():
        mqtt = FakeMqtt()
        t = MqttTransport(locker_id="L1", relay_id=5, client=mqtt)
        ok = await t.send_command({
            "type": "command", "command_id": "uuid-string", "relay_cmd_id": 7,
            "command_type": "open", "parameters": {},
        })
        assert ok is True
        topic, payload, qos, retain = mqtt.published[0]
        assert topic == topic_com(5)
        assert qos == 1
        assert retain is True
        assert len(payload) == 12  # com is 12 bytes
        # relay_cmd_id echoed in first 4 bytes (NOT the UUID command_id)
        assert int.from_bytes(payload[0:4], "little") == 7
    asyncio.run(scenario())


def test_mqtt_transport_broker_down_returns_false_without_publishing():
    async def scenario():
        mqtt = FakeMqtt(connected=False)
        t = MqttTransport(locker_id="L1", relay_id=5, client=mqtt)
        ok = await t.send_command({
            "type": "command", "relay_cmd_id": 7,
            "command_type": "open", "parameters": {},
        })
        assert ok is False
        assert mqtt.published == []
    asyncio.run(scenario())


def test_mqtt_transport_missing_relay_cmd_id_returns_false():
    async def scenario():
        mqtt = FakeMqtt()
        t = MqttTransport(locker_id="L1", relay_id=5, client=mqtt)
        ok = await t.send_command({
            "type": "command", "command_type": "open", "parameters": {},
        })
        # relay_cmd_id отсутствует → KeyError внутри → False, ничего не публикуем.
        assert ok is False
        assert mqtt.published == []
    asyncio.run(scenario())


def test_mqtt_transport_bad_command_type_returns_false():
    async def scenario():
        mqtt = FakeMqtt()
        t = MqttTransport(locker_id="L1", relay_id=5, client=mqtt)
        ok = await t.send_command({
            "type": "command", "relay_cmd_id": 7,
            "command_type": "nonsense", "parameters": {},
        })
        assert ok is False
        assert mqtt.published == []
    asyncio.run(scenario())


def test_mqtt_transport_state_command_is_retained():
    """open/close — state-команды: retain=True (восстановление состояния §6)."""
    async def scenario():
        for ctype in ("open", "close"):
            mqtt = FakeMqtt()
            t = MqttTransport(locker_id="L1", relay_id=5, client=mqtt)
            ok = await t.send_command({
                "type": "command", "relay_cmd_id": 1,
                "command_type": ctype, "parameters": {},
            })
            assert ok is True
            _topic, _payload, _qos, retain = mqtt.published[0]
            assert retain is True, f"{ctype} must be retained"
    asyncio.run(scenario())


def test_mqtt_transport_oneshot_command_is_not_retained():
    """reboot — one-shot: retain=False, иначе reboot переисполнится на reconnect."""
    async def scenario():
        mqtt = FakeMqtt()
        t = MqttTransport(locker_id="L1", relay_id=5, client=mqtt)
        ok = await t.send_command({
            "type": "command", "relay_cmd_id": 2,
            "command_type": "reboot", "parameters": {},
        })
        assert ok is True
        _topic, _payload, _qos, retain = mqtt.published[0]
        assert retain is False
    asyncio.run(scenario())


def test_mqtt_transport_relay_commands_not_retained_and_carry_channel():
    async def scenario():
        expected = {"relay_on": 1, "relay_off": 2, "relay_restart": 3}
        for ctype, code in expected.items():
            mqtt = FakeMqtt()
            t = MqttTransport(locker_id="L1", relay_id=5, client=mqtt)
            ok = await t.send_command({
                "type": "command", "relay_cmd_id": 9,
                "command_type": ctype, "parameters": {"relay": 3},
            })
            assert ok is True
            _topic, payload, _qos, retain = mqtt.published[0]
            assert retain is False, f"{ctype} must not be retained"
            assert int.from_bytes(payload[4:8], "little") == code
            assert int.from_bytes(payload[8:12], "little") == 3
    asyncio.run(scenario())


# --- prepare_send (transport-owned pre-send prep) -------------------------------

from types import SimpleNamespace  # noqa: E402


class FakeAllocator:
    """Минимальный RelayCmdIdAllocator-порт: только allocate_relay_cmd_id."""
    def __init__(self, relay_cmd_id=0xABCD):
        self.calls = []
        self._rid = relay_cmd_id

    async def allocate_relay_cmd_id(self, command_id, locker_id):
        self.calls.append((command_id, locker_id))
        return self._rid


def test_mqtt_prepare_send_allocates_and_injects():
    """MqttTransport.prepare_send выделяет relay_cmd_id через порт-аллокатор и кладёт в data."""
    async def scenario():
        allocator = FakeAllocator(relay_cmd_id=0x1234)
        t = MqttTransport(locker_id="L1", relay_id=5, client=FakeMqtt(),
                          allocator=allocator)
        command = SimpleNamespace(id="c1", locker_id="L1")
        data = {"type": "command"}
        await t.prepare_send(command, data)
        assert data["relay_cmd_id"] == 0x1234
        assert allocator.calls == [("c1", "L1")]
    asyncio.run(scenario())


def test_mqtt_prepare_send_reuses_existing_relay_cmd_id():
    """Если у команды уже есть relay_cmd_id (повтор после ack-timeout) — не аллоцируем."""
    async def scenario():
        allocator = FakeAllocator()
        t = MqttTransport(locker_id="L1", relay_id=5, client=FakeMqtt(),
                          allocator=allocator)
        command = SimpleNamespace(id="c1", locker_id="L1", relay_cmd_id=99)
        data = {"type": "command"}
        await t.prepare_send(command, data)
        assert data["relay_cmd_id"] == 99
        assert allocator.calls == []  # аллокатор не дёргается
    asyncio.run(scenario())


def test_mqtt_prepare_send_no_allocator_is_noop():
    """Без allocator (часть unit-тестов) prepare_send — no-op."""
    async def scenario():
        t = MqttTransport(locker_id="L1", relay_id=5, client=FakeMqtt())
        data = {"type": "command"}
        await t.prepare_send(SimpleNamespace(id="c1", locker_id="L1"), data)
        assert "relay_cmd_id" not in data
    asyncio.run(scenario())


from datetime import UTC, datetime  # noqa: E402

from app.locker_data.db.models.command import (  # noqa: E402
    CommandStatus,
    CommandType,
    LockerCommand,
)


def _locker_command(relay_cmd_id=None) -> LockerCommand:
    """Реальная доменная модель (slots-dataclass), не SimpleNamespace.

    Регрессия HIGH-1: модель ДОЛЖНА иметь поле relay_cmd_id, иначе
    конструктор падает на старом коде, а prepare_send всегда видит None.
    """
    return LockerCommand(
        id="c1", locker_id="L1", type=CommandType.OPEN,
        status=CommandStatus.SENT, parameters={}, created_at=datetime.now(UTC),
        created_by=None, sent_at=None, delivered_at=None, executed_at=None,
        failed_at=None, result={}, error_message=None, retry_count=1,
        max_retries=3, relay_cmd_id=relay_cmd_id,
    )


def test_mqtt_prepare_send_reuses_existing_on_real_model():
    """На РЕАЛЬНОЙ LockerCommand с relay_cmd_id повтор не перевыделяет id.

    SimpleNamespace-тест этого не ловил: getattr на slots-dataclass без поля
    всегда вернул бы None, и аллокатор дёргался бы каждый повтор."""
    async def scenario():
        allocator = FakeAllocator()
        t = MqttTransport(locker_id="L1", relay_id=5, client=FakeMqtt(),
                          allocator=allocator)
        data = {"type": "command"}
        await t.prepare_send(_locker_command(relay_cmd_id=42), data)
        assert data["relay_cmd_id"] == 42
        assert allocator.calls == []  # fast-path: аллокатор не вызывается
    asyncio.run(scenario())


def test_mqtt_prepare_send_allocates_on_real_model_without_id():
    """На РЕАЛЬНОЙ LockerCommand без relay_cmd_id — аллоцируем (первая отправка)."""
    async def scenario():
        allocator = FakeAllocator(relay_cmd_id=0x55)
        t = MqttTransport(locker_id="L1", relay_id=5, client=FakeMqtt(),
                          allocator=allocator)
        data = {"type": "command"}
        await t.prepare_send(_locker_command(relay_cmd_id=None), data)
        assert data["relay_cmd_id"] == 0x55
        assert allocator.calls == [("c1", "L1")]
    asyncio.run(scenario())
