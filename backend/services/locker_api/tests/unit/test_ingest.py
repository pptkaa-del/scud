import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

from app.locker_data.db.models.locker import Connectivity, DoorState, OperationalStatus
from app.mqtt.ingest import RelayIngest
from app.mqtt.relay_protocol import Command, RepResult, topic_com
from shared.enums import RelayMessageOutcome


class FakeRepo:
    def __init__(self, mapping, connectivity_by_id=None):
        self._by_relay = mapping  # relay_id -> locker_id
        self.door_updates = []   # командный путь (update_door) — телеметрия его НЕ зовёт
        self.door_syncs = []     # телеметрический путь (sync_door_state)
        self.door = {}           # locker_id -> DoorState в «БД»
        self.connectivity = []
        # locker_id -> текущая connectivity (для _is_offline в rep-пути).
        self._conn_by_id = dict(connectivity_by_id or {})
        # Operational status, возвращаемый get_operational_status. Default NORMAL
        # чтобы существующие тесты продолжали эмитить телеметрию без изменений.
        self.op_status = OperationalStatus.NORMAL

    async def get_locker_id_by_relay_id(self, relay_id):
        return self._by_relay.get(relay_id)

    async def update_connectivity(self, locker_id, value):
        self.connectivity.append((locker_id, value))
        self._conn_by_id[locker_id] = value

    async def update_door(self, locker_id, door, who_open_last=None):
        # Командный путь (CommandResultApplier) — авторитетное событие открытия.
        # Телеметрия (ingest heartbeat) НЕ должна его звать (иначе она сбрасывает
        # last_opened_at и затирает who_open_last каждые ~5 мин).
        self.door_updates.append((locker_id, door, who_open_last))

    async def sync_door_state(self, locker_id, door):
        # Edge-triggered, как реальный UPDATE … WHERE door <> %s RETURNING:
        # строка возвращается только при смене; новый шкаф создаётся закрытым.
        self.door_syncs.append((locker_id, door))
        if self.door.get(locker_id, DoorState.CLOSED) == door:
            return None
        self.door[locker_id] = door
        return SimpleNamespace(id=locker_id, door=door)

    async def get_by_id(self, locker_id):
        conn = self._conn_by_id.get(locker_id)
        if conn is None:
            return None
        return SimpleNamespace(id=locker_id, connectivity=conn)

    async def get_connectivity(self, locker_id):
        # Лёгкая проекция: ingest._is_offline дёргает только connectivity.
        return self._conn_by_id.get(locker_id)

    async def mark_offline_if_online(self, locker_id):
        # COR-19: атомарный флип online→offline. Возвращает True только если
        # ЭТОТ вызов реально перевёл шкаф (имитируем единственный UPDATE …
        # WHERE connectivity='online' RETURNING — повторный вызов проигрывает).
        if self._conn_by_id.get(locker_id) == Connectivity.ONLINE:
            self._conn_by_id[locker_id] = Connectivity.OFFLINE
            self.connectivity.append((locker_id, Connectivity.OFFLINE))
            return True
        return False

    async def get_operational_status(self, locker_id):
        return self.op_status

    async def raise_alarm(self, locker_id):
        if self.op_status != OperationalStatus.NORMAL:
            return None
        self.op_status = OperationalStatus.ALARM
        return SimpleNamespace(id=locker_id, inventory_number=f"INV-{locker_id}",
                               location_address="ул. Тестовая, 1")


class FakeQueue:
    def __init__(self):
        self.registered = {}
        self.unregistered = []
        self.register_calls = 0

    async def register_connection(self, locker_id, transport):
        self.registered[locker_id] = transport
        self.register_calls += 1

    async def unregister_connection(self, locker_id, transport=None):
        self.registered.pop(locker_id, None)
        self.unregistered.append(locker_id)

    def is_connected(self, locker_id):
        return locker_id in self.registered

    def get_transport(self, locker_id):
        return self.registered.get(locker_id)


class FakeMqtt:
    is_connected = True

    def __init__(self):
        self.published = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


class FakeOffline:
    def __init__(self):
        self.events = []

    async def publish_offline(self, locker_id, reason, last_seen_at=None):
        self.events.append((locker_id, reason))


class FakeResults:
    def __init__(self, outcome=RelayMessageOutcome.PROCESSED):
        self.calls = []
        self.received_at = []
        self._outcome = outcome

    async def handle_rep(self, locker_id, cmd_id, result, received_at=None):
        self.calls.append((locker_id, cmd_id, result))
        self.received_at.append(received_at)
        return self._outcome


def _build_ingest(mapping, telemetry=None, connectivity_by_id=None,
                  mqtt=None, offline=None):
    repo = FakeRepo(mapping, connectivity_by_id=connectivity_by_id)
    queue, results = FakeQueue(), FakeResults()
    ingest = RelayIngest(repo=repo, command_queue=queue, command_results=results,
                         mqtt_client=mqtt if mqtt is not None else object(),
                         telemetry=telemetry, offline_producer=offline)
    return ingest, repo, queue, results


def _u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def test_stat_online_registers_transport_and_sets_online():
    async def scenario():
        ingest, repo, queue, results = _build_ingest({5: "L5"})
        # v2 relay stat (online) for relay_id=5:
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))
        await ingest.handle_message(topic="stat", payload=raw)
        assert "L5" in queue.registered
        assert ("L5", Connectivity.ONLINE) in repo.connectivity
    asyncio.run(scenario())


def test_stat_offline_unregisters_transport_on_transition():
    """Findings 1+5: device-reported status=0 на online-шкафе помечает offline,
    СНИМАЕТ транспорт (иначе MqttTransport утечёт — offline-монитор матчит только
    connectivity='online' и шкаф уже не подберёт) и эмитит ровно ОДИН offline
    event. Делается только на переходе online→offline."""
    async def scenario():
        offline = FakeOffline()
        ingest, repo, queue, results = _build_ingest({5: "L5"}, offline=offline)
        # Сначала online-stat регистрирует транспорт (connectivity → ONLINE).
        online_raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1)
                      + _u32(1) + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1)
                      + _u32(20))
        await ingest.handle_message(topic="stat", payload=online_raw)
        assert "L5" in queue.registered
        # Затем stat с status=OFFLINE (0).
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(20))
        await ingest.handle_message(topic="stat", payload=raw)
        # Транспорт снят (маршрут к мёртвой плате не утекает).
        assert "L5" not in queue.registered
        assert queue.unregistered == ["L5"]
        # connectivity помечена offline и offline event эмитнут ровно один раз.
        assert ("L5", Connectivity.OFFLINE) in repo.connectivity
        assert offline.events == [("L5", "heartbeat_timeout")]
    asyncio.run(scenario())


def test_repeated_stat_offline_is_idempotent():
    """Finding 5: повторный/retained stat=0 на уже offline-шкафе НЕ плодит
    дубликат offline-события и не дёргает unregister второй раз."""
    async def scenario():
        offline = FakeOffline()
        ingest, repo, queue, results = _build_ingest(
            {5: "L5"}, connectivity_by_id={"L5": Connectivity.ONLINE}, offline=offline)
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(20))
        # Первый stat=0: online→offline переход.
        await ingest.handle_message(topic="stat", payload=raw)
        assert queue.unregistered == ["L5"]
        assert offline.events == [("L5", "heartbeat_timeout")]
        # Второй stat=0: шкаф уже offline → no-op (идемпотентность).
        await ingest.handle_message(topic="stat", payload=raw)
        assert queue.unregistered == ["L5"]               # без повторного unregister
        assert offline.events == [("L5", "heartbeat_timeout")]  # без дубликата
        # connectivity не переписывалась повторно.
        assert repo.connectivity.count(("L5", Connectivity.OFFLINE)) == 1
    asyncio.run(scenario())


def test_stat_from_unknown_relay_is_dropped():
    async def scenario():
        ingest, repo, queue, results = _build_ingest({5: "L5"})
        raw = (_u32(99) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))
        await ingest.handle_message(topic="stat", payload=raw)
        assert queue.registered == {}
        assert repo.connectivity == []
    asyncio.run(scenario())


def test_malformed_frame_is_swallowed():
    async def scenario():
        ingest, repo, queue, results = _build_ingest({5: "L5"})
        # Too short to decode → ValueError, must be logged+dropped (no raise).
        await ingest.handle_message(topic="stat", payload=b"\x00\x00")
        assert queue.registered == {}
    asyncio.run(scenario())


def test_rep_routes_to_command_results():
    async def scenario():
        ingest, repo, queue, results = _build_ingest({5: "L5"})
        rep_raw = _u32(5) + _u32(7) + _u32(int(RepResult.SUCCESS))
        await ingest.handle_message(topic="rep", payload=rep_raw)
        assert results.calls == [("L5", 7, RepResult.SUCCESS)]
    asyncio.run(scenario())


class FakeTelemetry:
    def __init__(self):
        self.batches = []

    async def publish_batch(self, locker_id, readings):
        self.batches.append((locker_id, list(readings)))


class FakeSensors:
    """Резолвит (locker_id, code) → детерминированный UUID, создавая по запросу.

    Эмулирует SensorRepository.get_or_create_id_by_type_code: первый запрос
    "создаёт" датчик (запоминает serial_number), повторный отдаёт тот же UUID.
    """
    def __init__(self):
        self.created = {}  # (locker_id, code) -> (uuid, serial_number)
        self.calls = []
        self.values = []  # (sensor_id, value) пары, записанные update_value/swap_value
        self.current = {}  # sensor_id -> последнее записанное значение

    async def get_or_create_id_by_type_code(self, locker_id, sensor_type_code, serial_number):
        self.calls.append((locker_id, sensor_type_code, serial_number))
        key = (locker_id, sensor_type_code)
        if key not in self.created:
            sid = str(uuid.UUID(int=len(self.created) + 1))
            self.created[key] = (sid, serial_number)
        return self.created[key][0]

    async def update_value(self, sensor_id, value):
        self.values.append((sensor_id, value))
        self.current[sensor_id] = value

    async def swap_value(self, sensor_id, value):
        previous = self.current.get(sensor_id)
        if sensor_id in self.current and previous == value and type(previous) is type(value):
            return False, None
        await self.update_value(sensor_id, value)
        return True, previous

    async def get_id_by_locker_and_type_code(self, locker_id, sensor_type_code):
        return self.created.get((locker_id, sensor_type_code), (None,))[0]


def _build_ingest_with_sensors(mapping, telemetry=None):
    repo = FakeRepo(mapping)
    queue, results = FakeQueue(), FakeResults()
    sensors = FakeSensors()
    ingest = RelayIngest(repo=repo, command_queue=queue, command_results=results,
                         mqtt_client=object(), telemetry=telemetry,
                         offline_producer=None, sensor_repo=sensors)
    return ingest, sensors


def test_stat_v2_emits_battery_and_signal_telemetry_with_uuid_sensor_id():
    """Fix BUG 1: sensor_id в reading — реальный UUID, а не "<relay_id>:battery"."""
    async def scenario():
        telemetry = FakeTelemetry()
        ingest, sensors = _build_ingest_with_sensors({5: "L5"}, telemetry=telemetry)

        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(77) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(25))
        await ingest.handle_message(topic="stat", payload=raw)
        assert telemetry.batches, "expected telemetry enqueued"
        locker_id, readings = telemetry.batches[0]
        assert locker_id == "L5"
        by_code = {r["sensor_type_code"]: r for r in readings}
        assert {"battery", "signal", "door", "power"}.issubset(set(by_code))
        assert by_code["battery"]["value"] == 77
        assert by_code["signal"]["value"] == 25
        # sensor_id ДОЛЖЕН быть валидным UUID (history_api требует UUID-колонку).
        for code in ("battery", "signal"):
            sid = by_code[code]["sensor_id"]
            uuid.UUID(sid)  # бросит ValueError если не UUID — это и есть проверка
            assert ":" not in sid  # старый синтетический "<relay_id>:battery" недопустим
        # Serial относится к локеру, тип резолвится по коду.
        serials = {c[1]: c[2] for c in sensors.calls}
        assert serials["battery"] == "relay-battery"
        assert serials["signal"] == "relay-signal"
    asyncio.run(scenario())


def test_repeated_stat_resolves_sensor_id_from_cache():
    """Повторные stat'ы не дёргают get_or_create на каждый — резолв кэшируется."""
    async def scenario():
        telemetry = FakeTelemetry()
        ingest, sensors = _build_ingest_with_sensors({5: "L5"}, telemetry=telemetry)
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(77) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(25))
        await ingest.handle_message(topic="stat", payload=raw)
        await ingest.handle_message(topic="stat", payload=raw)
        # 6 кодов × 1 резолв (второй stat бьёт в кэш) = 6 вызовов, не 12.
        assert len(sensors.calls) == 6
        # sensor_id стабилен между батчами; error пишется только на изменении,
        # поэтому во втором батче его нет.
        _, first = telemetry.batches[0]
        _, second = telemetry.batches[1]
        first_ids = {r["sensor_id"] for r in first}
        second_ids = {r["sensor_id"] for r in second}
        error_id = sensors.created[("L5", "error")][0]
        assert second_ids <= first_ids
        assert error_id in first_ids
        assert error_id not in second_ids
    asyncio.run(scenario())


def test_stat_v2_updates_door_state():
    """Телеметрия синхронизирует door через sync_door_state (НЕ командный
    update_door), поэтому heartbeat-stat не сбрасывает last_opened_at /
    who_open_last. Регрессия бага «дверь переоткрывается каждые 5 минут»."""
    async def scenario():
        ingest, repo, queue, results = _build_ingest({5: "L5"})
        # door = CLOSED (2)
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))
        await ingest.handle_message(topic="stat", payload=raw)
        assert repo.door_syncs, "expected door sync via telemetry path"
        locker_id, door = repo.door_syncs[0]
        assert locker_id == "L5"
        assert door.value == "closed"
        # Телеметрия НИКОГДА не идёт через командный update_door (тот трогает
        # last_opened_at/who_open_last).
        assert repo.door_updates == []
    asyncio.run(scenario())


def test_repeated_stat_does_not_re_register(monkeypatch):
    """Fix 3: heartbeat-stat'ы НЕ должны повторно вызывать register_connection.

    Первый stat регистрирует транспорт; повторные stat'ы того же relay_id лишь
    обновляют connectivity (last_seen_at), без register-churn.
    """
    async def scenario():
        ingest, repo, queue, results = _build_ingest({5: "L5"})
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))
        await ingest.handle_message(topic="stat", payload=raw)
        await ingest.handle_message(topic="stat", payload=raw)
        await ingest.handle_message(topic="stat", payload=raw)
        # Один register на три stat'а.
        assert queue.register_calls == 1
        # Но connectivity(ONLINE) обновлялась на каждом stat (last_seen_at refresh).
        online = [c for c in repo.connectivity if c[1] == Connectivity.ONLINE]
        assert len(online) == 3
    asyncio.run(scenario())


def test_stat_offline_emits_offline_event():
    """Findings 1+5: inline stat=0 на online-шкафе эмитит offline history
    event и снимает транспорт (атомарный online→offline флип, COR-19)."""
    async def scenario():
        offline = FakeOffline()
        ingest, repo, queue, results = _build_ingest(
            {5: "L5"}, connectivity_by_id={"L5": Connectivity.ONLINE}, offline=offline)
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(20))
        await ingest.handle_message(topic="stat", payload=raw)
        assert offline.events
        locker_id, reason = offline.events[0]
        assert locker_id == "L5"
        assert reason == "heartbeat_timeout"
        assert queue.unregistered == ["L5"]
    asyncio.run(scenario())


def test_concurrent_stat_offline_emits_once():
    """COR-19: два stat=0 для одного online-шкафа (retained + live). Атомарный
    UPDATE … WHERE connectivity='online' RETURNING гарантирует, что переход
    выигрывает РОВНО один вызов: один OFFLINE event + один unregister. Второй
    наблюдает 0 строк (mark_offline_if_online → False) и ничего не делает."""
    async def scenario():
        offline = FakeOffline()
        ingest, repo, queue, results = _build_ingest(
            {5: "L5"}, connectivity_by_id={"L5": Connectivity.ONLINE}, offline=offline)
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(20))
        # Два последовательных stat=0 (имитируют interleave retained+live).
        await ingest.handle_message(topic="stat", payload=raw)
        await ingest.handle_message(topic="stat", payload=raw)
        # Ровно один offline event и ровно один unregister.
        assert offline.events == [("L5", "heartbeat_timeout")]
        assert queue.unregistered == ["L5"]
        # connectivity флипнута offline ровно один раз.
        assert repo.connectivity.count(("L5", Connectivity.OFFLINE)) == 1
    asyncio.run(scenario())


def test_rep_from_offline_locker_requeries_status():
    """Fix 6: rep от offline-шкафа → publish SEND_STATUS (command=4) в com/{id}."""
    async def scenario():
        mqtt = FakeMqtt()
        ingest, repo, queue, results = _build_ingest(
            {5: "L5"}, connectivity_by_id={"L5": Connectivity.OFFLINE}, mqtt=mqtt)
        rep_raw = _u32(5) + _u32(7) + _u32(int(RepResult.SUCCESS))
        await ingest.handle_message(topic="rep", payload=rep_raw)
        # rep всё равно ушёл в results.
        assert results.calls == [("L5", 7, RepResult.SUCCESS)]
        # И опубликован re-query SEND_STATUS, retain=False.
        assert mqtt.published
        topic, payload, _qos, retain = mqtt.published[0]
        assert topic == topic_com(5)
        assert retain is False
        assert int.from_bytes(payload[0:4], "little") == 0       # cmd_id=0
        assert int.from_bytes(payload[4:8], "little") == int(Command.SEND_STATUS)
        assert int.from_bytes(payload[8:12], "little") == 0       # data=0
    asyncio.run(scenario())


def test_stale_rep_on_offline_locker_still_requeries():
    """Любой rep (и stale тоже) доказывает, что плата жива → SEND_STATUS."""
    async def scenario():
        mqtt = FakeMqtt()
        ingest, repo, queue, results = _build_ingest(
            {5: "L5"}, connectivity_by_id={"L5": Connectivity.OFFLINE}, mqtt=mqtt)
        results._outcome = RelayMessageOutcome.STALE_REP
        rep_raw = _u32(5) + _u32(7) + _u32(int(RepResult.SUCCESS))
        await ingest.handle_message(topic="rep", payload=rep_raw)
        assert len(mqtt.published) == 1
        assert mqtt.published[0][0] == topic_com(5)
    asyncio.run(scenario())


def test_handle_message_passes_received_at_to_handle_rep():
    async def scenario():
        ingest, repo, queue, results = _build_ingest({5: "L5"})
        rep_raw = _u32(5) + _u32(7) + _u32(int(RepResult.SUCCESS))
        at = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
        await ingest.handle_message("rep", rep_raw, received_at=at)
        await ingest.handle_message("rep", rep_raw)
        assert results.received_at[0] == at
        assert results.received_at[1] is not None
        assert results.received_at[1].tzinfo is not None
    asyncio.run(scenario())


def test_rep_from_online_locker_does_not_requery():
    """Fix 6: rep от online-шкафа НЕ публикует re-query."""
    async def scenario():
        mqtt = FakeMqtt()
        ingest, repo, queue, results = _build_ingest(
            {5: "L5"}, connectivity_by_id={"L5": Connectivity.ONLINE}, mqtt=mqtt)
        rep_raw = _u32(5) + _u32(7) + _u32(int(RepResult.SUCCESS))
        await ingest.handle_message(topic="rep", payload=rep_raw)
        assert results.calls == [("L5", 7, RepResult.SUCCESS)]
        assert mqtt.published == []
    asyncio.run(scenario())


def test_photo_routed_to_stub_no_raise():
    async def scenario():
        ingest, repo, queue, results = _build_ingest({5: "L5"})

        jpeg = b"\xff\xd8\xff\xe0body"
        raw = (_u32(5) + _u32(42) + _u32(3) + _u32(1) + _u32(len(jpeg)) + jpeg)
        # Should resolve locker + log, not raise.
        await ingest.handle_message(topic="photos", payload=raw)
        # photos are not telemetry/connectivity events
        assert repo.connectivity == []
    asyncio.run(scenario())


def test_oversized_photo_is_swallowed_as_malformed():
    """SEC-04: photo declaring a body above MAX_RELAY_PHOTO_BYTES must surface as
    a handled decode error (logged + dropped) on the detached MQTT task, NOT an
    unhandled exception. The codec rejects on declared size alone, so no huge
    allocation occurs."""
    from app.mqtt import relay_codec as rc

    async def scenario():
        ingest, repo, queue, results = _build_ingest({5: "L5"})
        oversize = rc.MAX_RELAY_PHOTO_BYTES + 1
        raw = (_u32(5) + _u32(42) + _u32(3) + _u32(1) + _u32(oversize))
        # Must not raise out of handle_message (detached task would crash otherwise).
        await ingest.handle_message(topic="photos", payload=raw)
        assert repo.connectivity == []
    asyncio.run(scenario())


def test_stat_v2_without_relay_block_emits_signal_telemetry():
    async def scenario():
        telemetry = FakeTelemetry()
        ingest, _ = _build_ingest_with_sensors({5: "L5"}, telemetry=telemetry)
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(77) + _u32(2) + _u32(0)
               + _u32(2) * 4 + _u32(1) + _u32(23))
        await ingest.handle_message(topic="stat", payload=raw)
        _, readings = telemetry.batches[0]
        by_code = {r["sensor_type_code"]: r["value"] for r in readings}
        assert by_code["signal"] == 23
    asyncio.run(scenario())


def test_stat_v2_without_relay_block_error_status_emits_security_event():
    async def scenario():
        factory = FakeSecurityFactory()
        ingest = RelayIngest(repo=FakeRepo({14: "L14"}), command_queue=FakeQueue(),
                             command_results=FakeResults(), mqtt_client=object(),
                             telemetry=FakeTelemetry(), offline_producer=None,
                             sensor_repo=FakeSensors(), security_producer_factory=factory)
        raw = (_u32(14) + _u32(2) + _u32(2) + _u32(1) + _u32(77) + _u32(1) + _u32(0)
               + _u32(2) * 4 + _u32(2) + _u32(0))
        await ingest.handle_message("stat", raw)
        assert [(c[0], c[1]) for c in factory.calls] == [("L14", "error")]
    asyncio.run(scenario())


def test_stat_updates_current_value_for_battery_and_signal():
    """A1: каждый appended reading должен также вызывать update_value (WS-паритет)."""
    async def scenario():
        ingest, sensors = _build_ingest_with_sensors({5: "L5"}, telemetry=FakeTelemetry())
        # v2 relay stat (online), battery=80, signal=20:
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))
        await ingest.handle_message(topic="stat", payload=raw)
        assert 80 in [v for _, v in sensors.values]
        assert 20 in [v for _, v in sensors.values]
        # error, battery, signal, door, power, net_type
        assert len(sensors.values) == 6
    asyncio.run(scenario())


def test_stat_emits_door_and_power_readings():
    """A2: телеметрический батч включает door (bool) и power (int) наряду с battery/signal."""
    async def scenario():
        ingest, sensors = _build_ingest_with_sensors({7: "L7"}, telemetry=FakeTelemetry())
        # v2 relay stat: door=OPEN(1), power=MAINS(1), battery=90, signal=15
        raw = (_u32(7) + _u32(2) + _u32(1) + _u32(1) + _u32(90) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(15))
        await ingest.handle_message(topic="stat", payload=raw)
        # door (True->1.0) and power (1) both produced update_value calls
        recorded = [v for _, v in sensors.values]
        assert any(v is True or v == 1 for v in recorded)   # door open
        assert 90 in recorded                               # battery still there
        assert 15 in recorded                               # signal still there
        assert len(sensors.values) == 6  # error, battery, signal, door, power, net_type
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Security-event fakes (Task 4, 5, 6)
# ---------------------------------------------------------------------------

class FakeSecurityProducer:
    def __init__(self, sink, uid_sink):
        self.sink = sink
        self.uid_sink = uid_sink

    async def publish(self, locker_id, sensor_id, violations, *,
                      sensor_type_code=None, raw_value=None, event_uid=None,
                      extra_metadata=None):
        self.sink.append((locker_id, sensor_type_code, tuple(violations)))
        self.uid_sink.append(event_uid)
        return 1


class FakeSecurityFactory:
    def __init__(self):
        self.calls = []
        self.event_uids = []  # event_uid passed to .publish(), index-aligned with calls

    def __call__(self, code):
        return FakeSecurityProducer(self.calls, self.event_uids)


# ---------------------------------------------------------------------------
# Task 4 (A3): alarm=1 → tamper security event
# ---------------------------------------------------------------------------

def test_v1_stat_alarm_emits_tamper_security_event():
    """A3: device-asserted alarm=1 emits a tamper security event."""
    async def scenario():
        repo = FakeRepo({9: "L9"})
        queue, results = FakeQueue(), FakeResults()
        sensors = FakeSensors()
        factory = FakeSecurityFactory()
        ingest = RelayIngest(repo=repo, command_queue=queue, command_results=results,
                             mqtt_client=object(), telemetry=FakeTelemetry(),
                             offline_producer=None, sensor_repo=sensors,
                             security_producer_factory=factory)
        # v1 stat: id=9, version=1, relays[4]=0, status=ONLINE(1), signal=20, alarm=1
        raw = (_u32(9) + _u32(1) + _u32(0) + _u32(0) + _u32(0) + _u32(0)
               + _u32(1) + _u32(20) + _u32(1))
        await ingest.handle_message(topic="stat", payload=raw)
        assert len(factory.calls) == 1
        locker_id, code, violations = factory.calls[0]
        assert locker_id == "L9"
        assert code == "tamper"
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Task 5 (A4): MAINS→BATTERY transition emits power-loss event exactly once
# ---------------------------------------------------------------------------

def test_power_transition_to_battery_emits_event_once():
    """A4: MAINS→BATTERY fires one power security event; subsequent BATTERY stats are quiet."""
    async def scenario():
        repo = FakeRepo({11: "L11"})
        sensors = FakeSensors()
        factory = FakeSecurityFactory()
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                             mqtt_client=object(), telemetry=FakeTelemetry(),
                             offline_producer=None, sensor_repo=sensors,
                             security_producer_factory=factory)

        def stat(power):  # v2 relay_en=1, door=CLOSED(2), battery=50, signal=20
            return (_u32(11) + _u32(2) + _u32(2) + _u32(power) + _u32(50) + _u32(1) + _u32(1)
                    + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))

        await ingest.handle_message("stat", stat(1))  # MAINS
        await ingest.handle_message("stat", stat(0))  # -> BATTERY (transition)
        await ingest.handle_message("stat", stat(0))  # still BATTERY (no repeat)
        power_events = [c for c in factory.calls if c[1] == "power"]
        assert len(power_events) == 1
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Task 6 (A5): DeviceStatus.ERROR emits a security event (factory code=None)
# ---------------------------------------------------------------------------

def test_stat_error_status_emits_security_event():
    """A5: relay self-reported ERROR still registers online but also emits a security event."""
    async def scenario():
        repo = FakeRepo({13: "L13"})
        factory = FakeSecurityFactory()
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                             mqtt_client=object(), telemetry=FakeTelemetry(),
                             offline_producer=None, sensor_repo=FakeSensors(),
                             security_producer_factory=factory)
        # v1 stat with status=ERROR(2): id=13, version=1, relays[4]=0, status=2, signal=20, alarm=0
        raw = (_u32(13) + _u32(1) + _u32(0) + _u32(0) + _u32(0) + _u32(0)
               + _u32(2) + _u32(20) + _u32(0))
        await ingest.handle_message("stat", raw)
        assert len(factory.calls) == 1
        locker_id, code, _ = factory.calls[0]
        assert locker_id == "L13"
        assert code == "error"
    asyncio.run(scenario())


def test_emit_security_never_raises_on_factory_failure():
    async def scenario():
        class BoomFactory:
            def __call__(self, code):
                raise RuntimeError("db down")
        repo = FakeRepo({9: "L9"})
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                             mqtt_client=object(), telemetry=FakeTelemetry(),
                             offline_producer=None, sensor_repo=FakeSensors(),
                             security_producer_factory=BoomFactory())
        # v1 stat with alarm=1 → would invoke the factory; must NOT raise.
        raw = (_u32(9) + _u32(1) + _u32(0) + _u32(0) + _u32(0) + _u32(0)
               + _u32(1) + _u32(20) + _u32(1))
        await ingest.handle_message(topic="stat", payload=raw)  # no exception = pass
        # connectivity still updated despite the security-emit failure
        assert ("L9", Connectivity.ONLINE) in repo.connectivity
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Task 7 (A6): MAINTENANCE operational status suppresses telemetry
# ---------------------------------------------------------------------------

def test_maintenance_suppresses_telemetry():
    """A6: telemetry (publish_batch + update_value) is skipped when locker is in
    MAINTENANCE; connectivity update and all other event checks still run."""
    async def scenario():
        repo = FakeRepo({15: "L15"})
        repo.op_status = OperationalStatus.MAINTENANCE
        telemetry = FakeTelemetry()
        sensors = FakeSensors()
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                             mqtt_client=object(), telemetry=telemetry,
                             offline_producer=None, sensor_repo=sensors)
        raw = (_u32(15) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))
        await ingest.handle_message("stat", raw)
        # Обычная телеметрия подавлена; состояния платы (ошибка, дверь) пишутся и в MAINTENANCE.
        error_sid = sensors.created[("L15", "error")][0]
        door_sid = sensors.created[("L15", "door")][0]
        assert sensors.values == [(error_sid, False), (door_sid, False)]
        assert len(telemetry.batches) == 1
        locker_id, readings = telemetry.batches[0]
        assert locker_id == "L15"
        assert [(r["sensor_type_code"], r["value"]) for r in readings] == [("error", 0.0)]
        assert ("L15", Connectivity.ONLINE) in repo.connectivity  # connectivity still updated
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Task 8 (A7): per-device telemetry rate-limit (TokenBucket)
# ---------------------------------------------------------------------------

def test_repeated_alarm_emits_tamper_once_until_cleared():
    """A3 edge-trigger: alarm=1 на каждом heartbeat не плодит tamper-событие;
    только переход 0→1 эмитит. После сброса в 0 следующий 0→1 снова эмитит."""
    async def scenario():
        repo = FakeRepo({9: "L9"})
        factory = FakeSecurityFactory()
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                             mqtt_client=object(), telemetry=FakeTelemetry(),
                             offline_producer=None, sensor_repo=FakeSensors(),
                             security_producer_factory=factory)
        def v1(alarm):  # id=9, version=1, relays[4]=0, status=ONLINE(1), signal=20, alarm
            return (_u32(9) + _u32(1) + _u32(0) + _u32(0) + _u32(0) + _u32(0)
                    + _u32(1) + _u32(20) + _u32(alarm))
        await ingest.handle_message("stat", v1(1))   # 0->1 emit
        await ingest.handle_message("stat", v1(1))   # held, no emit
        await ingest.handle_message("stat", v1(0))   # cleared
        await ingest.handle_message("stat", v1(1))   # 0->1 emit again
        tamper = [c for c in factory.calls if c[1] == "tamper"]
        assert len(tamper) == 2
    asyncio.run(scenario())


def test_repeated_error_emits_event_once_until_recovered():
    """A5 edge-trigger: status=ERROR на каждом heartbeat не плодит security-событие;
    только переход ok→ERROR эмитит. Восстановление в ONLINE re-arms триггер."""
    async def scenario():
        repo = FakeRepo({13: "L13"})
        factory = FakeSecurityFactory()
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                             mqtt_client=object(), telemetry=FakeTelemetry(),
                             offline_producer=None, sensor_repo=FakeSensors(),
                             security_producer_factory=factory)
        def v1(status):  # id=13, version=1, relays[4]=0, status, signal=20, alarm=0
            return (_u32(13) + _u32(1) + _u32(0) + _u32(0) + _u32(0) + _u32(0)
                    + _u32(status) + _u32(20) + _u32(0))
        await ingest.handle_message("stat", v1(2))   # ERROR: ok->ERROR emit
        await ingest.handle_message("stat", v1(2))   # held ERROR, no emit
        await ingest.handle_message("stat", v1(1))   # ONLINE: recover
        await ingest.handle_message("stat", v1(2))   # ERROR again: emit
        errors = [c for c in factory.calls
                  if c[2] == ("Устройство сообщило об ошибке (статус ERROR)",)]
        assert len(errors) == 2
    asyncio.run(scenario())


def test_telemetry_rate_limited_per_device():
    """A7: second stat from same device is dropped when bucket is exhausted
    (burst=1); connectivity and events are unaffected."""
    async def scenario():
        repo = FakeRepo({17: "L17"})
        telemetry = FakeTelemetry()
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                             mqtt_client=object(), telemetry=telemetry,
                             offline_producer=None, sensor_repo=FakeSensors(),
                             telemetry_rate=1.0, telemetry_burst=1.0)  # 1 token, slow refill
        raw = (_u32(17) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))
        await ingest.handle_message("stat", raw)   # consumes the 1 token -> published
        await ingest.handle_message("stat", raw)   # bucket empty -> suppressed
        assert len(telemetry.batches) == 1
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# net_type и датчик ошибки платы (stat == ERROR)
# ---------------------------------------------------------------------------

def _stat_v2(relay_id, *, power=1, net_type=1, status=1, door=2, battery=80, signal=20):
    return (_u32(relay_id) + _u32(2) + _u32(door) + _u32(power) + _u32(battery)
            + _u32(net_type) + _u32(1) + _u32(0) * 4 + _u32(status) + _u32(signal))


def _error_values(sensors, locker_id):
    sid = sensors.created.get((locker_id, "error"), (None,))[0]
    return [v for s, v in sensors.values if s == sid]


def test_net_type_reading_written_raw_without_dropping_frame():
    async def scenario():
        for raw_net in (0, 1, 2, 99):
            telemetry = FakeTelemetry()
            ingest, sensors = _build_ingest_with_sensors({5: "L5"}, telemetry=telemetry)
            await ingest.handle_message("stat", _stat_v2(5, net_type=raw_net))
            _, readings = telemetry.batches[0]
            by_code = {r["sensor_type_code"]: r for r in readings}
            assert by_code["net_type"]["value"] == float(raw_net)
            net_sid, serial = sensors.created[("L5", "net_type")]
            assert serial == "relay-net_type"
            written = [v for s, v in sensors.values if s == net_sid]
            assert written == [raw_net]
            assert type(written[0]) is int
    asyncio.run(scenario())


def test_v1_stat_has_no_net_type_reading():
    async def scenario():
        telemetry = FakeTelemetry()
        ingest, sensors = _build_ingest_with_sensors({9: "L9"}, telemetry=telemetry)
        raw = (_u32(9) + _u32(1) + _u32(0) * 4 + _u32(1) + _u32(20) + _u32(0))
        await ingest.handle_message("stat", raw)
        codes = {r["sensor_type_code"] for _, batch in telemetry.batches for r in batch}
        assert "net_type" not in codes
        assert ("L9", "net_type") not in sensors.created
    asyncio.run(scenario())


def test_error_sensor_is_edge_triggered():
    async def scenario():
        telemetry = FakeTelemetry()
        ingest, sensors = _build_ingest_with_sensors({5: "L5"}, telemetry=telemetry)
        await ingest.handle_message("stat", _stat_v2(5, status=1))
        assert _error_values(sensors, "L5") == [False]
        await ingest.handle_message("stat", _stat_v2(5, status=1))
        assert _error_values(sensors, "L5") == [False]
        await ingest.handle_message("stat", _stat_v2(5, status=2))
        assert _error_values(sensors, "L5") == [False, True]
        await ingest.handle_message("stat", _stat_v2(5, status=2))
        assert _error_values(sensors, "L5") == [False, True]
        await ingest.handle_message("stat", _stat_v2(5, status=1))
        assert _error_values(sensors, "L5") == [False, True, False]
        assert all(type(v) is bool for v in _error_values(sensors, "L5"))
    asyncio.run(scenario())


def test_error_sensor_keeps_last_value_on_offline_stat():
    async def scenario():
        ingest, sensors = _build_ingest_with_sensors({5: "L5"}, telemetry=FakeTelemetry())
        await ingest.handle_message("stat", _stat_v2(5, status=2))
        await ingest.handle_message("stat", _stat_v2(5, status=0))
        assert _error_values(sensors, "L5") == [True]
    asyncio.run(scenario())


def test_error_sensor_written_in_maintenance():
    async def scenario():
        repo = FakeRepo({15: "L15"})
        repo.op_status = OperationalStatus.MAINTENANCE
        telemetry, sensors = FakeTelemetry(), FakeSensors()
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                             mqtt_client=object(), telemetry=telemetry,
                             offline_producer=None, sensor_repo=sensors)
        await ingest.handle_message("stat", _stat_v2(15, status=2))
        error_sid = sensors.created[("L15", "error")][0]
        door_sid = sensors.created[("L15", "door")][0]
        assert sensors.values == [(error_sid, True), (door_sid, False)]
        assert [[(r["sensor_type_code"], r["value"]) for r in batch]
                for _, batch in telemetry.batches] == [[("error", 1.0)]]
    asyncio.run(scenario())


def test_error_sensor_written_when_telemetry_bucket_empty():
    async def scenario():
        repo = FakeRepo({17: "L17"})
        telemetry, sensors = FakeTelemetry(), FakeSensors()
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                             mqtt_client=object(), telemetry=telemetry,
                             offline_producer=None, sensor_repo=sensors,
                             telemetry_rate=0.001, telemetry_burst=1.0)
        await ingest.handle_message("stat", _stat_v2(17, status=1))
        await ingest.handle_message("stat", _stat_v2(17, status=2, battery=10))
        assert _error_values(sensors, "L17") == [False, True]
        battery_sid = sensors.created[("L17", "battery")][0]
        assert [v for s, v in sensors.values if s == battery_sid] == [80]
        assert len(telemetry.batches) == 2
        assert [r["sensor_type_code"] for r in telemetry.batches[1][1]] == ["error"]
    asyncio.run(scenario())


def test_short_v2_frame_without_status_does_not_touch_error_sensor():
    async def scenario():
        ingest, sensors = _build_ingest_with_sensors({5: "L5"}, telemetry=FakeTelemetry())
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(0))
        await ingest.handle_message("stat", raw)
        assert ("L5", "error") not in sensors.created
        assert ("L5", "battery") in sensors.created
    asyncio.run(scenario())


def test_error_sensor_retried_after_failed_resolve():
    async def scenario():
        class FlakySensors(FakeSensors):
            def __init__(self):
                super().__init__()
                self.fail_error = True

            async def get_or_create_id_by_type_code(self, locker_id, sensor_type_code,
                                                    serial_number):
                if sensor_type_code == "error" and self.fail_error:
                    return None
                return await super().get_or_create_id_by_type_code(
                    locker_id, sensor_type_code, serial_number)

        sensors = FlakySensors()
        ingest = RelayIngest(repo=FakeRepo({5: "L5"}), command_queue=FakeQueue(),
                             command_results=FakeResults(), mqtt_client=object(),
                             telemetry=FakeTelemetry(), offline_producer=None,
                             sensor_repo=sensors)
        await ingest.handle_message("stat", _stat_v2(5, status=1))
        assert ("L5", "error") not in sensors.created
        sensors.fail_error = False
        await ingest.handle_message("stat", _stat_v2(5, status=1))
        assert _error_values(sensors, "L5") == [False]
    asyncio.run(scenario())


def test_error_security_event_carries_error_sensor_id():
    async def scenario():
        captured = []

        class Producer:
            async def publish(self, locker_id, sensor_id, violations, *,
                              sensor_type_code=None, event_uid=None, extra_metadata=None):
                captured.append((sensor_type_code, sensor_id))

        sensors = FakeSensors()
        ingest = RelayIngest(repo=FakeRepo({5: "L5"}), command_queue=FakeQueue(),
                             command_results=FakeResults(), mqtt_client=object(),
                             telemetry=FakeTelemetry(), offline_producer=None,
                             sensor_repo=sensors,
                             security_producer_factory=lambda code: Producer())
        await ingest.handle_message("stat", _stat_v2(5, status=2))
        assert captured == [("error", sensors.created[("L5", "error")][0])]
    asyncio.run(scenario())


def test_error_sensor_consistent_under_concurrent_stats():
    """Две конкурентные stat'ы одной платы: commit в БД и обновление кэша не
    должны расходиться, иначе edge-кэш навсегда залипает на чужом значении."""
    async def scenario():
        class GatedSensors(FakeSensors):
            def __init__(self):
                super().__init__()
                self.gates = {True: asyncio.Event(), False: asyncio.Event()}
                self.gated = True

            async def update_value(self, sensor_id, value):
                await super().update_value(sensor_id, value)
                if self.gated and type(value) is bool:
                    await self.gates[value].wait()

        sensors = GatedSensors()
        ingest = RelayIngest(repo=FakeRepo({5: "L5"}), command_queue=FakeQueue(),
                             command_results=FakeResults(), mqtt_client=object(),
                             telemetry=FakeTelemetry(), offline_producer=None,
                             sensor_repo=sensors)
        ok_task = asyncio.create_task(ingest.handle_message("stat", _stat_v2(5, status=1)))
        err_task = asyncio.create_task(ingest.handle_message("stat", _stat_v2(5, status=2)))
        for _ in range(10):
            await asyncio.sleep(0)
        sensors.gates[True].set()
        for _ in range(10):
            await asyncio.sleep(0)
        sensors.gates[False].set()
        await asyncio.gather(ok_task, err_task)

        sensors.gated = False
        for _ in range(3):
            await ingest.handle_message("stat", _stat_v2(5, status=1))
        assert _error_values(sensors, "L5")[-1] is False
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Несанкционированное открытие двери (протокол §7.1): дверь закрыто→открыто,
# а действующая команда замка не «Открыть» → intrusion + датчик «Взлом».
# ---------------------------------------------------------------------------

_DOOR_OPEN, _DOOR_CLOSED, _DOOR_UNDEFINED = 1, 2, 0
_T0 = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


class FakeCommands:
    """is_open_authorized: bool или исключение; gate — придержать ответ."""

    def __init__(self, authorized=False):
        self.authorized = authorized
        self.calls = []
        self.gate = None

    async def is_open_authorized(self, locker_id, at, rejected_reasons):
        self.calls.append((locker_id, at, tuple(rejected_reasons)))
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.authorized, Exception):
            raise self.authorized
        return self.authorized


class FakeNotifier:
    def __init__(self, fail=False):
        self.created = []
        self.fail = fail

    async def create_persistent(self, text, tags, roles=None, user_ids=None):
        if self.fail:
            raise RuntimeError("notification_api down")
        self.created.append({"text": text, "tags": tags, "roles": roles})
        return "n1"


def _build_door_ingest(*, authorized=False, op_status=OperationalStatus.NORMAL,
                       notifier=None):
    repo = FakeRepo({5: "L5"})
    repo.op_status = op_status
    commands = FakeCommands(authorized)
    sensors, factory, telemetry = FakeSensors(), FakeSecurityFactory(), FakeTelemetry()
    ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                         mqtt_client=object(), telemetry=telemetry, offline_producer=None,
                         sensor_repo=sensors, security_producer_factory=factory,
                         command_repo=commands,
                         notification_client=notifier if notifier is not None else FakeNotifier())
    return ingest, repo, commands, sensors, factory, telemetry


def _values(sensors, locker_id, code):
    sid = sensors.created.get((locker_id, code), (None,))[0]
    return [v for s, v in sensors.values if s == sid]


def _intrusion_events(factory):
    return [c for c in factory.calls if c[1] == "intrusion"]


async def _door(ingest, *states):
    for state in states:
        await ingest.handle_message("stat", _stat_v2(5, door=state))


def test_unauthorized_door_open_emits_intrusion_and_sets_sensor():
    async def scenario():
        ingest, _, commands, sensors, factory, telemetry = _build_door_ingest()
        await _door(ingest, _DOOR_CLOSED)
        await ingest.handle_message("stat", _stat_v2(5, door=_DOOR_OPEN), received_at=_T0)
        events = _intrusion_events(factory)
        assert len(events) == 1
        assert events[0][0] == "L5"
        assert "без команды" in events[0][2][0]
        assert commands.calls == [("L5", _T0, ("failed", "not_supported"))]
        assert _values(sensors, "L5", "intrusion") == [True]
        intrusion_id = sensors.created[("L5", "intrusion")][0]
        published = [r for _, batch in telemetry.batches for r in batch
                     if r["sensor_id"] == intrusion_id]
        assert [r["value"] for r in published] == [1.0]
    asyncio.run(scenario())


def test_authorized_door_open_is_not_intrusion():
    async def scenario():
        ingest, _, commands, sensors, factory, _ = _build_door_ingest(authorized=True)
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN)
        assert _intrusion_events(factory) == []
        assert _values(sensors, "L5", "intrusion") == []
        assert len(commands.calls) == 1
    asyncio.run(scenario())


def test_first_observed_door_open_is_not_intrusion():
    """Прежнего состояния двери ещё нет (новый шкаф) — §7.1 не выполнено."""
    async def scenario():
        ingest, _, commands, _, factory, _ = _build_door_ingest()
        await _door(ingest, _DOOR_OPEN)
        assert _intrusion_events(factory) == []
        assert commands.calls == []
    asyncio.run(scenario())


def test_heartbeat_with_open_door_does_not_repeat_intrusion():
    async def scenario():
        ingest, _, commands, sensors, factory, _ = _build_door_ingest()
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN, _DOOR_OPEN, _DOOR_OPEN)
        assert len(_intrusion_events(factory)) == 1
        assert len(commands.calls) == 1
        assert _values(sensors, "L5", "intrusion") == [True]
    asyncio.run(scenario())


def test_door_close_clears_intrusion_sensor_and_rearms():
    async def scenario():
        ingest, _, commands, sensors, factory, _ = _build_door_ingest()
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN, _DOOR_CLOSED, _DOOR_OPEN)
        assert len(_intrusion_events(factory)) == 2
        assert _values(sensors, "L5", "intrusion") == [True, False, True]
        assert len(commands.calls) == 2
    asyncio.run(scenario())


def test_close_command_with_open_door_is_not_intrusion():
    """rep CLOSE переписал lockers.door в closed, а дверь физически открыта:
    следующий stat door=open — не переход по наблюдаемому состоянию."""
    async def scenario():
        ingest, repo, commands, _, factory, _ = _build_door_ingest(authorized=True)
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN)
        commands.authorized = False
        repo.door["L5"] = DoorState.CLOSED
        await _door(ingest, _DOOR_OPEN)
        assert _intrusion_events(factory) == []
        assert repo.door["L5"] == DoorState.OPEN
    asyncio.run(scenario())


def test_close_rep_before_close_stat_still_clears_intrusion():
    async def scenario():
        ingest, repo, _, sensors, factory, _ = _build_door_ingest()
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN)
        repo.door["L5"] = DoorState.CLOSED
        await _door(ingest, _DOOR_CLOSED)
        assert _values(sensors, "L5", "intrusion") == [True, False]
        assert len(_intrusion_events(factory)) == 1
    asyncio.run(scenario())


def test_door_close_without_prior_intrusion_does_not_touch_sensor():
    async def scenario():
        ingest, _, _, sensors, factory, _ = _build_door_ingest(authorized=True)
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN, _DOOR_CLOSED, _DOOR_CLOSED)
        assert _intrusion_events(factory) == []
        assert ("L5", "intrusion") not in sensors.created
    asyncio.run(scenario())


def test_undefined_door_state_is_ignored():
    async def scenario():
        ingest, repo, _, sensors, factory, _ = _build_door_ingest()
        await _door(ingest, _DOOR_CLOSED, _DOOR_UNDEFINED, _DOOR_OPEN)
        assert repo.door_syncs == [("L5", DoorState.CLOSED), ("L5", DoorState.OPEN)]
        assert len(_intrusion_events(factory)) == 1
        assert _values(sensors, "L5", "door") == [False, True]
    asyncio.run(scenario())


def test_intrusion_in_maintenance_is_logged_without_alarm():
    async def scenario():
        notifier = FakeNotifier()
        ingest, repo, _, sensors, factory, _ = _build_door_ingest(
            op_status=OperationalStatus.MAINTENANCE, notifier=notifier)
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN)
        assert len(_intrusion_events(factory)) == 1
        assert _values(sensors, "L5", "intrusion") == [True]
        assert _values(sensors, "L5", "door") == [False, True]
        assert repo.op_status == OperationalStatus.MAINTENANCE
        assert notifier.created == []
    asyncio.run(scenario())


def test_unauthorized_open_raises_alarm_and_notifies_operators():
    async def scenario():
        notifier = FakeNotifier()
        ingest, repo, _, _, _, _ = _build_door_ingest(notifier=notifier)
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN)
        assert repo.op_status == OperationalStatus.ALARM
        assert len(notifier.created) == 1
        note = notifier.created[0]
        assert note["text"].startswith("Взлом: шкаф INV-L5 (ул. Тестовая, 1) открыт без")
        assert note["tags"] == {"event": "intrusion", "locker_id": "L5",
                                "dismiss_by": "operator_or_service"}
        assert note["roles"] == ["operator", "administrator"]
    asyncio.run(scenario())


def test_repeated_intrusion_in_alarm_is_logged_without_new_notification():
    async def scenario():
        notifier = FakeNotifier()
        ingest, repo, _, _, factory, _ = _build_door_ingest(notifier=notifier)
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN, _DOOR_CLOSED, _DOOR_OPEN)
        assert len(_intrusion_events(factory)) == 2
        assert len(notifier.created) == 1
        repo.op_status = OperationalStatus.NORMAL
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN)
        assert len(notifier.created) == 2
    asyncio.run(scenario())


def test_authorized_open_raises_no_alarm():
    async def scenario():
        notifier = FakeNotifier()
        ingest, repo, _, _, _, _ = _build_door_ingest(authorized=True, notifier=notifier)
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN)
        assert repo.op_status == OperationalStatus.NORMAL
        assert notifier.created == []
    asyncio.run(scenario())


def test_notification_failure_keeps_alarm_and_event():
    async def scenario():
        ingest, repo, _, sensors, factory, _ = _build_door_ingest(
            notifier=FakeNotifier(fail=True))
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN, _DOOR_CLOSED)
        assert repo.op_status == OperationalStatus.ALARM
        assert len(_intrusion_events(factory)) == 1
        assert _values(sensors, "L5", "intrusion") == [True, False]
    asyncio.run(scenario())


def test_authority_lookup_failure_raises_alarm():
    """Не смогли проверить команду — считаем открытие несанкционированным:
    ложная тревога дешевле пропущенного взлома."""
    async def scenario():
        ingest, _, _, sensors, factory, _ = _build_door_ingest(
            authorized=RuntimeError("db down"))
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN)
        assert len(_intrusion_events(factory)) == 1
        assert _values(sensors, "L5", "intrusion") == [True]
    asyncio.run(scenario())


def test_stale_stat_does_not_rewind_door():
    """stat, пришедший раньше уже применённого, дверь не трогает."""
    async def scenario():
        ingest, repo, commands, sensors, factory, _ = _build_door_ingest()
        await ingest._handle_stat(_stat_v2(5, door=_DOOR_CLOSED), _T0, 2)
        await ingest._handle_stat(_stat_v2(5, door=_DOOR_OPEN), _T0, 1)
        assert _values(sensors, "L5", "door") == [False]
        assert repo.door_syncs == [("L5", DoorState.CLOSED)]
        assert commands.calls == []
        assert _intrusion_events(factory) == []
    asyncio.run(scenario())


def test_without_command_repo_door_is_synced_without_detection():
    async def scenario():
        repo, sensors, factory = FakeRepo({5: "L5"}), FakeSensors(), FakeSecurityFactory()
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=FakeResults(),
                             mqtt_client=object(), telemetry=FakeTelemetry(),
                             offline_producer=None, sensor_repo=sensors,
                             security_producer_factory=factory)
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN)
        assert repo.door_syncs == [("L5", DoorState.CLOSED), ("L5", DoorState.OPEN)]
        assert _values(sensors, "L5", "door") == [False, True]
        assert _intrusion_events(factory) == []
    asyncio.run(scenario())


def test_concurrent_open_and_close_leave_intrusion_sensor_consistent():
    """Медленная проверка команды для open не должна перезаписать False от
    следующего close: итог датчика совпадает с последним состоянием двери."""
    async def scenario():
        ingest, _, commands, sensors, factory, _ = _build_door_ingest()
        await _door(ingest, _DOOR_CLOSED)
        commands.gate = asyncio.Event()
        open_task = asyncio.create_task(
            ingest.handle_message("stat", _stat_v2(5, door=_DOOR_OPEN)))
        for _ in range(10):
            await asyncio.sleep(0)
        close_task = asyncio.create_task(
            ingest.handle_message("stat", _stat_v2(5, door=_DOOR_CLOSED)))
        for _ in range(10):
            await asyncio.sleep(0)
        commands.gate.set()
        await asyncio.gather(open_task, close_task)
        assert len(_intrusion_events(factory)) == 1
        assert _values(sensors, "L5", "intrusion")[-1] is False
    asyncio.run(scenario())


def test_close_after_restart_clears_existing_intrusion_sensor():
    async def scenario():
        ingest, repo, _, sensors, factory, _ = _build_door_ingest()
        sid = await sensors.get_or_create_id_by_type_code("L5", "intrusion", "relay-intrusion")
        await sensors.update_value(sid, True)
        door_sid = await sensors.get_or_create_id_by_type_code("L5", "door", "relay-door")
        await sensors.update_value(door_sid, True)
        repo.door["L5"] = DoorState.OPEN
        await _door(ingest, _DOOR_CLOSED)
        assert [v for s, v in sensors.values if s == sid] == [True, False]
        assert _intrusion_events(factory) == []
    asyncio.run(scenario())


def test_closed_heartbeat_clears_stale_intrusion_sensor():
    async def scenario():
        ingest, _, _, sensors, factory, _ = _build_door_ingest()
        sid = await sensors.get_or_create_id_by_type_code("L5", "intrusion", "relay-intrusion")
        await sensors.update_value(sid, True)
        door_sid = await sensors.get_or_create_id_by_type_code("L5", "door", "relay-door")
        await sensors.update_value(door_sid, False)
        await _door(ingest, _DOOR_CLOSED)
        assert [v for s, v in sensors.values if s == sid] == [True, False]
        assert _intrusion_events(factory) == []
    asyncio.run(scenario())


def test_repeated_closed_heartbeats_do_not_rewrite_intrusion_sensor():
    async def scenario():
        ingest, _, _, sensors, _, telemetry = _build_door_ingest()
        await _door(ingest, _DOOR_CLOSED, _DOOR_OPEN, _DOOR_CLOSED, _DOOR_CLOSED, _DOOR_CLOSED)
        assert _values(sensors, "L5", "intrusion") == [True, False]
        intrusion_id = sensors.created[("L5", "intrusion")][0]
        published = [r for _, batch in telemetry.batches for r in batch
                     if r["sensor_id"] == intrusion_id]
        assert len(published) == 2
    asyncio.run(scenario())
