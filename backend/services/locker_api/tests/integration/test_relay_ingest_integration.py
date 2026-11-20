"""Integration test: relay stat write-through to sensors.current_value (A1).

Proves that a real RelayIngest wired with real LockerRepository /
SensorRepository / TelemetryProducer against the test DB actually persists
current_value after processing a relay stat frame — end-to-end proof that
Task 2 (A1: set sensor current_value on every telemetry reading) actually
writes through to the database, not merely calls update_value on a stub.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import psycopg2
import pytest

from app.locker_data.db.repositories.command import CommandRepository
from app.locker_data.db.repositories.locker import LockerRepository
from app.locker_data.db.repositories.sensor import SensorRepository, _unpack_value
from app.mqtt.ingest import RelayIngest
from app.services.history_producers.security_event_producer import producer_for_sensor_type
from app.services.history_producers.telemetry_producer import TelemetryProducer
from shared.database import AsyncPostgres, PostgresConfig
from shared.enums import RelayMessageOutcome

# ---------------------------------------------------------------------------
# DB helpers (mirror test_relay_mqtt_flow.py)
# ---------------------------------------------------------------------------

def _db_config() -> PostgresConfig:
    return PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        database=os.getenv("DB_NAME", "scud"),
    )


def _sync_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud"),
    )


def _cleanup_locker(locker_id: str) -> None:
    """Hard-delete locker; cascade removes sensors and commands."""
    conn = _sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db():
    """AsyncPostgres pool for real-DB tests; closed after each test."""
    pg = AsyncPostgres(_db_config())
    try:
        yield pg
    finally:
        pg.close()


# ---------------------------------------------------------------------------
# Minimal stubs (only the CommandQueue / Results / Mqtt methods used by
# _handle_stat on the online path — no command dispatch, no rep handling)
# ---------------------------------------------------------------------------

class _StubQueue:
    """Stand-in for CommandQueue: stores registered transports, no dispatch."""

    def __init__(self) -> None:
        self._registered: dict[str, object] = {}

    async def register_connection(self, locker_id: str, transport: object) -> None:
        self._registered[locker_id] = transport

    async def unregister_connection(self, locker_id: str,
                                    transport: object = None) -> None:
        self._registered.pop(locker_id, None)

    def is_connected(self, locker_id: str) -> bool:
        return locker_id in self._registered

    def get_transport(self, locker_id: str) -> object | None:
        return self._registered.get(locker_id)


class _StubResults:
    async def handle_rep(self, locker_id: str, cmd_id: int,
                         result: object, received_at: object = None) -> RelayMessageOutcome:
        return RelayMessageOutcome.PROCESSED


class _StubMqtt:
    is_connected = True

    async def publish(self, topic: str, payload: bytes,
                      qos: int = 0, retain: bool = False) -> None:
        pass


# ---------------------------------------------------------------------------
# Frame builder
# ---------------------------------------------------------------------------

def _u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def _build_stat_v2(relay_id: int, *, battery: int = 70, signal: int = 18,
                   power: int = 1, net_type: int = 1, status: int = 1,
                   door: int = 2) -> bytes:
    """Build a 52-byte v2 relay stat (relay_en=1, status=ONLINE).

    Field layout mirrors relay_codec.decode_stat (uint32 LE):
    id, version=2, door=CLOSED(2), power=MAINS(1), battery, net_type=1,
    relay_en=1, r1..r4=0, stat=ONLINE(1), signal.
    """
    return (
        _u32(relay_id)   # device_id
        + _u32(2)        # version = V2
        + _u32(door)     # door: 1 = OPEN, 2 = CLOSED
        + _u32(power)    # power (MAINS=1)
        + _u32(battery)  # battery level
        + _u32(net_type)
        + _u32(1)        # relay_en = 1 (relay block present)
        + _u32(0) + _u32(0) + _u32(0) + _u32(0)  # r1..r4
        + _u32(status)   # status (ONLINE=1)
        + _u32(signal)   # signal level
    )


# ---------------------------------------------------------------------------
# Ingest factory wired to the test DB
# ---------------------------------------------------------------------------

def _build_real_ingest(db: AsyncPostgres) -> RelayIngest:
    """Construct RelayIngest with real repositories against the test DB.

    command_queue / command_results / mqtt_client are minimal stubs:
    _handle_stat on the online path calls register_connection (stub handles it)
    and update_connectivity (real LockerRepository); it does not dispatch
    commands or publish MQTT for a simple stat, so stubs suffice here.
    """
    return RelayIngest(
        repo=LockerRepository(db),
        command_queue=_StubQueue(),
        command_results=_StubResults(),
        mqtt_client=_StubMqtt(),
        telemetry=TelemetryProducer(db),
        offline_producer=None,
        sensor_repo=SensorRepository(db),
        allocator=None,
        security_producer_factory=lambda code: producer_for_sensor_type(db, code),
        command_repo=CommandRepository(db),
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_relay_stat_populates_current_value(db):
    """Relay stat through real RelayIngest writes current_value to sensors row.

    End-to-end proof of A1: unit tests prove update_value is *called*; this
    proves it *persists* — the battery value from the stat frame must appear
    in sensors.current_value after handle_message returns.

    The battery sensor is auto-created on the first stat via
    get_or_create_id_by_type_code (sensor_types.code='battery' is seeded in
    001_init.sql, so no manual sensor_type row is needed).
    """
    suffix = uuid.uuid4().hex[:8]
    relay_id = 4700000 + int(suffix[:4], 16) % 90000
    locker_id: str | None = None

    async def scenario() -> None:
        nonlocal locker_id
        repo = LockerRepository(db)
        locker = await repo.create(
            inventory_number=f"INT-BATT-{suffix}", created_by=None,
            transport="mqtt", relay_id=relay_id,
        )
        locker_id = locker.id

        ingest = _build_real_ingest(db)
        raw = _build_stat_v2(relay_id, battery=10, signal=15)
        await ingest.handle_message("stat", raw)

        sensor_repo = SensorRepository(db)
        sid = await sensor_repo.get_id_by_locker_and_type_code(locker.id, "battery")
        assert sid is not None, (
            "battery sensor was not auto-created on first stat — "
            "check that sensor_types has code='battery' (seeded in 001_init.sql)"
        )
        sensor = await sensor_repo.get_by_id(sid)
        assert sensor is not None
        # _unpack_value is called once by get_by_id (_row_to_sensor); calling it
        # again on the already-unpacked value is a no-op for numeric/int results.
        assert _unpack_value(sensor.current_value) == 10, (
            f"expected current_value=10, got {sensor.current_value!r}"
        )

    try:
        asyncio.run(scenario())
    finally:
        if locker_id is not None:
            _cleanup_locker(locker_id)


def test_relay_stat_sensors_carry_display(db):
    """power/net_type/error пишутся сырыми значениями и читаются с display."""
    suffix = uuid.uuid4().hex[:8]
    relay_id = 4800000 + int(suffix[:4], 16) % 90000
    locker_id: str | None = None

    async def scenario() -> None:
        nonlocal locker_id
        repo = LockerRepository(db)
        locker = await repo.create(
            inventory_number=f"INT-DISP-{suffix}", created_by=None,
            transport="mqtt", relay_id=relay_id,
        )
        locker_id = locker.id
        ingest = _build_real_ingest(db)
        sensor_repo = SensorRepository(db)

        await ingest.handle_message(
            "stat", _build_stat_v2(relay_id, power=0, net_type=1, status=2))
        by_code = {s.code: s for s in await sensor_repo.get_by_locker(locker.id)}

        power = by_code["power"]
        assert power.current_value == 0
        assert power.unit is None
        assert power.display is not None
        assert (power.display.label, power.display.severity) == ("От батареи", "warning")

        net_type = by_code["net_type"]
        assert net_type.current_value == 1
        assert net_type.display is not None
        assert (net_type.display.label, net_type.display.severity) == ("Ethernet", "normal")

        error = by_code["error"]
        assert error.current_value is True
        assert error.serial_number == "relay-error"
        assert error.display is not None
        assert (error.display.label, error.display.severity) == ("Ошибка", "alert")

        assert by_code["battery"].display is None

        await ingest.handle_message("stat", _build_stat_v2(relay_id, status=1))
        error = next(s for s in await sensor_repo.get_by_locker(locker.id)
                     if s.code == "error")
        assert error.current_value is False
        assert error.display is not None
        assert (error.display.label, error.display.severity) == ("Норма", "normal")

    try:
        asyncio.run(scenario())
    finally:
        if locker_id is not None:
            _cleanup_locker(locker_id)


_DOOR_OPEN, _DOOR_CLOSED = 1, 2


def _intrusion_outbox(locker_id: str) -> list[dict]:
    conn = _sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM outbound_history_events "
                "WHERE locker_id = %s AND channel = 'security_event' "
                "AND payload->>'event_type' = 'intrusion' ORDER BY id",
                (locker_id,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _cleanup_outbox(locker_id: str) -> None:
    conn = _sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM outbound_history_events WHERE locker_id = %s",
                        (locker_id,))
        conn.commit()
    finally:
        conn.close()


def test_unauthorized_door_open_raises_intrusion(db):
    suffix = uuid.uuid4().hex[:8]
    relay_id = 4900000 + int(suffix[:4], 16) % 90000
    locker_id: str | None = None

    async def scenario() -> None:
        nonlocal locker_id
        locker = await LockerRepository(db).create(
            inventory_number=f"INT-INTR-{suffix}", created_by=None,
            transport="mqtt", relay_id=relay_id,
        )
        locker_id = locker.id
        ingest = _build_real_ingest(db)
        sensor_repo = SensorRepository(db)

        await ingest.handle_message("stat", _build_stat_v2(relay_id, door=_DOOR_CLOSED))
        await ingest.handle_message("stat", _build_stat_v2(relay_id, door=_DOOR_OPEN))
        await ingest.handle_message("stat", _build_stat_v2(relay_id, door=_DOOR_OPEN))

        events = _intrusion_outbox(locker.id)
        assert len(events) == 1, events
        assert events[0]["severity"] == "high"
        intrusion = next(s for s in await sensor_repo.get_by_locker(locker.id)
                         if s.code == "intrusion")
        assert events[0]["sensor_id"] == str(intrusion.id)
        assert intrusion.current_value is True
        assert intrusion.display is not None
        assert (intrusion.display.label, intrusion.display.severity) == ("Взлом", "alert")

        await ingest.handle_message("stat", _build_stat_v2(relay_id, door=_DOOR_CLOSED))
        intrusion = next(s for s in await sensor_repo.get_by_locker(locker.id)
                         if s.code == "intrusion")
        assert intrusion.current_value is False
        assert intrusion.display is not None
        assert intrusion.display.label == "Норма"
        assert len(_intrusion_outbox(locker.id)) == 1
        assert (await LockerRepository(db).get_by_id(locker.id)).operational_status == "alarm"

    try:
        asyncio.run(scenario())
    finally:
        if locker_id is not None:
            _cleanup_outbox(locker_id)
            _cleanup_locker(locker_id)


def test_door_open_after_executed_open_command_is_authorized(db):
    suffix = uuid.uuid4().hex[:8]
    relay_id = 5000000 + int(suffix[:4], 16) % 90000
    locker_id: str | None = None

    async def scenario() -> None:
        nonlocal locker_id
        locker = await LockerRepository(db).create(
            inventory_number=f"INT-AUTH-{suffix}", created_by=None,
            transport="mqtt", relay_id=relay_id,
        )
        locker_id = locker.id
        conn = _sync_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO locker_commands (locker_id, type, status, sent_at, executed_at) "
                    "VALUES (%s, 'open', 'executed', now(), now())",
                    (locker.id,),
                )
            conn.commit()
        finally:
            conn.close()

        ingest = _build_real_ingest(db)
        await ingest.handle_message("stat", _build_stat_v2(relay_id, door=_DOOR_CLOSED))
        await ingest.handle_message("stat", _build_stat_v2(relay_id, door=_DOOR_OPEN))

        assert _intrusion_outbox(locker.id) == []
        codes = {s.code for s in await SensorRepository(db).get_by_locker(locker.id)}
        assert "intrusion" not in codes

    try:
        asyncio.run(scenario())
    finally:
        if locker_id is not None:
            _cleanup_outbox(locker_id)
            _cleanup_locker(locker_id)
