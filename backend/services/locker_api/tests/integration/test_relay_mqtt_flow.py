"""Real-DB integration тесты relay/MQTT слоя identity (Plan 2 Tasks 5 + 5b).

Покрывают:
- LockerRepository.create(transport='mqtt', relay_id=...) + get_by_relay_id;
- CommandRepository.allocate_relay_cmd_id / resolve_relay_cmd_id корреляцию rep.

Тесты идут напрямую через AsyncPostgres (как остальные integration-тесты
locker_api), без HTTP-сервиса. Фикстура `db` отдаёт AsyncPostgres и закрывает
пул; каждый тест чистит созданные строки, чтобы партиальный уникальный индекс
ux_lockers_relay_id не ломал повторные прогоны.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import uuid

import psycopg2
import pytest

from app.locker_data.db.repositories.command import CommandRepository
from app.locker_data.db.repositories.locker import LockerRepository
from app.mqtt.relay_protocol import RepResult
from app.mqtt.results import RelayCommandResults
from app.services.command_expiry import COMMAND_TTL_FAILURE_REASON
from app.services.history_producers.command_history_producer import CommandHistoryProducer
from app.services.history_producers.security_event_producer import (
    CommandLateExecutionProducer,
)
from shared.database import AsyncPostgres, PostgresConfig
from shared.enums import RelayMessageOutcome
from shared.mqtt_client import MqttClient, MqttQoS


def _db_config() -> PostgresConfig:
    return PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        database=os.getenv("DB_NAME", "scud"),
    )


def _sync_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud"),
    )


def _cleanup_locker(locker_id: str) -> None:
    """Hard-delete локера (cascade удалит и команды)."""
    conn = _sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db():
    """AsyncPostgres-пул для real-DB тестов; закрывается после теста."""
    pg = AsyncPostgres(_db_config())
    try:
        yield pg
    finally:
        pg.close()


def test_get_by_relay_id_roundtrip(db):
    suffix = uuid.uuid4().hex[:8]
    relay_id = 4200000 + int(suffix[:4], 16) % 90000  # уникален в пределах прогона
    created_id: str | None = None

    async def scenario():
        nonlocal created_id
        repo = LockerRepository(db)
        locker = await repo.create(
            inventory_number=f"RELAY-TEST-{suffix}", created_by=None,
            transport="mqtt", relay_id=relay_id,
        )
        created_id = locker.id
        assert locker.transport.value == "mqtt"
        assert locker.relay_id == relay_id

        found = await repo.get_by_relay_id(relay_id)
        assert found is not None
        assert found.id == locker.id
        assert found.relay_id == relay_id

        assert await repo.get_by_relay_id(999999) is None

        lid = await repo.get_locker_id_by_relay_id(relay_id)
        assert lid == locker.id

    try:
        asyncio.run(scenario())
    finally:
        if created_id is not None:
            _cleanup_locker(created_id)


def test_offline_monitor_flips_stale_mqtt_locker(db):
    """Поведенческий тест OfflineMonitor.sql_mark_stale_offline (Task 12).

    Вставляем mqtt-шкаф с connectivity='online' и last_seen_at в далёком
    прошлом, прогоняем UPDATE...RETURNING с порогом 350s и проверяем, что
    шкаф реально стал offline, а свежий online-шкаф — нет.
    """
    from app.mqtt.offline_monitor import OfflineMonitor

    suffix = uuid.uuid4().hex[:8]
    stale_relay = 4400000 + int(suffix[:4], 16) % 90000
    fresh_relay = 4500000 + int(suffix[:4], 16) % 90000
    stale_id: str | None = None
    fresh_id: str | None = None

    async def scenario():
        nonlocal stale_id, fresh_id
        repo = LockerRepository(db)
        stale = await repo.create(
            inventory_number=f"RELAY-STALE-{suffix}", created_by=None,
            transport="mqtt", relay_id=stale_relay,
        )
        fresh = await repo.create(
            inventory_number=f"RELAY-FRESH-{suffix}", created_by=None,
            transport="mqtt", relay_id=fresh_relay,
        )
        stale_id = stale.id
        fresh_id = fresh.id

        # Force both online; stale one's last_seen_at well past the threshold,
        # fresh one's last_seen_at = now().
        await db.execute(
            "UPDATE lockers SET connectivity = 'online', "
            "last_seen_at = now() - make_interval(secs => 1000) WHERE id = %s",
            (stale.id,),
        )
        await db.execute(
            "UPDATE lockers SET connectivity = 'online', last_seen_at = now() WHERE id = %s",
            (fresh.id,),
        )

        # Collect ids the monitor would touch (does NOT unregister anything since
        # neither locker is registered in this command-queue double).

        class _NoopQueue:
            async def unregister_connection(self, locker_id, transport=None):
                return None

        monitor = OfflineMonitor(db, _NoopQueue(), timeout_s=350, interval_s=1.0)
        flipped = set(await monitor._sweep_once())
        assert stale.id in flipped
        assert fresh.id not in flipped

        stale_row = await db.fetch_one(
            "SELECT connectivity FROM lockers WHERE id = %s", (stale.id,))
        fresh_row = await db.fetch_one(
            "SELECT connectivity FROM lockers WHERE id = %s", (fresh.id,))
        assert stale_row["connectivity"] == "offline"
        assert fresh_row["connectivity"] == "online"

    try:
        asyncio.run(scenario())
    finally:
        if stale_id is not None:
            _cleanup_locker(stale_id)
        if fresh_id is not None:
            _cleanup_locker(fresh_id)


def test_relay_cmd_id_allocate_and_resolve(db):
    suffix = uuid.uuid4().hex[:8]
    relay_id = 4300000 + int(suffix[:4], 16) % 90000
    created_id: str | None = None

    async def scenario():
        nonlocal created_id
        lrepo = LockerRepository(db)
        crepo = CommandRepository(db)
        locker = await lrepo.create(
            inventory_number=f"RELAY-CMD-{suffix}", created_by=None,
            transport="mqtt", relay_id=relay_id,
        )
        created_id = locker.id

        cmd = await crepo.create(
            locker_id=locker.id, command_type="open", created_by=None, parameters={},
        )
        rid = await crepo.allocate_relay_cmd_id(cmd.id, locker.id)
        assert isinstance(rid, int)
        assert 0 < rid <= 0xFFFFFFFF

        assert await crepo.resolve_relay_cmd_id(locker.id, rid) == cmd.id
        assert await crepo.resolve_relay_cmd_id(locker.id, 999999) is None

    try:
        asyncio.run(scenario())
    finally:
        if created_id is not None:
            _cleanup_locker(created_id)


def _u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def _build_v2_online_stat(relay_id: int) -> bytes:
    """Соберём v2 `stat` (relay_en=1, status=online) для fake-устройства.

    Раскладка совпадает с relay_codec.decode_stat (uint32 LE):
    id, version=2, door, power, battery, net_type, relay_en=1,
    r1, r2, r3, r4, stat=1 (online), sl. Итого 13×u32 = 52 байта
    (STAT_V2_RELAY_LEN).
    """
    return (
        _u32(relay_id)   # device_id
        + _u32(2)        # version = V2
        + _u32(2)        # door = CLOSED (DoorStateRaw.CLOSED)
        + _u32(1)        # power = MAINS
        + _u32(70)       # battery
        + _u32(1)        # net_type
        + _u32(1)        # relay_en = 1 (relay block present)
        + _u32(0) + _u32(0) + _u32(0) + _u32(0)  # r1..r4
        + _u32(1)        # stat = ONLINE
        + _u32(18)       # sl (signal)
    )


def test_relay_stat_brings_locker_online(db):
    """E2E: fake-устройство публикует v2 `stat` (online) в брокер →
    РАБОТАЮЩИЙ locker_api MQTT-ingest помечает шкаф online (Task 15).

    Тест шарит брокер `mqtt` и БД `scud_lockers` c уже запущенным
    locker_api: создаём mqtt-шкаф напрямую через repo.create (без API,
    без per-device provisioning), публикуем stat под общим broker-юзером
    `scud_backend` и поллим repo.get_by_id, пока connectivity не станет
    online. relay_id рандомизирован, чтобы партиальный уникальный индекс
    ux_lockers_relay_id не ломал повторные прогоны.
    """
    suffix = uuid.uuid4().hex[:8]
    relay_id = 4600000 + int(suffix[:4], 16) % 90000
    created_id: str | None = None

    async def scenario():
        nonlocal created_id
        repo = LockerRepository(db)
        locker = await repo.create(
            inventory_number=f"RELAY-MQTT-{suffix}", created_by=None,
            transport="mqtt", relay_id=relay_id,
        )
        created_id = locker.id

        # Fake-устройство: publish-only MqttClient (gmqtt-обёртка, без ручного loop).
        dev = MqttClient(
            os.getenv("MQTT_HOST", "mqtt"),
            int(os.getenv("MQTT_PORT", "1883")),
            f"relay-id{relay_id}-v2",
            username=os.getenv("MQTT_USER", "scud_backend"),
            password=os.getenv("MQTT_PASSWORD", "scud_backend_dev_password"),
            keepalive=30,
        )
        await dev.connect()
        try:
            stat = _build_v2_online_stat(relay_id)
            await dev.publish("stat", stat, qos=MqttQoS.AT_LEAST_ONCE, retain=True)

            # Работающий ingest должен пометить шкаф online в течение ~10s.
            refreshed = None
            iterations = 0
            for _ in range(50):
                iterations += 1
                refreshed = await repo.get_by_id(locker.id)
                if refreshed is not None and refreshed.connectivity == "online":
                    break
                await asyncio.sleep(0.2)
            assert refreshed is not None
            assert refreshed.connectivity == "online", (
                f"locker did not go online after {iterations} polls "
                f"(connectivity={refreshed.connectivity})"
            )
        finally:
            await dev.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        if created_id is not None:
            _cleanup_locker(created_id)


# --- терминальная команда: stale rep / command_late_execution --------------

class _RecordingNotifier:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_persistent(self, text, tags, roles=None, user_ids=None):
        self.created.append({"text": text, "tags": tags})
        return "n1"

    async def cancel_locker_open_notifications(self, locker_id):
        return 0


def _outbox(locker_id: str, channel: str) -> list[dict]:
    conn = _sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM outbound_history_events "
                "WHERE locker_id = %s AND channel = %s ORDER BY id",
                (locker_id, channel),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _exec_sql(sql: str, params: tuple) -> None:
    conn = _sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


async def _relay_command(db, suffix: str, base: int):
    lrepo = LockerRepository(db)
    crepo = CommandRepository(db)
    relay_id = base + int(suffix[:4], 16) % 90000
    locker = await lrepo.create(
        inventory_number=f"RELAY-LATE-{suffix}", created_by=None,
        transport="mqtt", relay_id=relay_id,
    )
    cmd = await crepo.create(
        locker_id=locker.id, command_type="open", created_by=7, parameters={},
    )
    rid = await crepo.allocate_relay_cmd_id(cmd.id, locker.id)
    return locker, cmd, rid


def _results(db, notifier=None) -> RelayCommandResults:
    return RelayCommandResults(
        command_repo=CommandRepository(db),
        history_producer=CommandHistoryProducer(db),
        locker_repo=LockerRepository(db),
        notification_client=notifier or _RecordingNotifier(),
        command_ttl_s=60,
        late_execution_producer=CommandLateExecutionProducer(db),
    )


def test_late_success_after_failed_keeps_failed_and_enqueues_one_security_event(db):
    suffix = uuid.uuid4().hex[:8]
    created_id: str | None = None
    notifier = _RecordingNotifier()

    async def scenario():
        nonlocal created_id
        locker, cmd, rid = await _relay_command(db, suffix, 4400000)
        created_id = locker.id
        _exec_sql(
            "UPDATE locker_commands SET status = 'failed', failed_at = now(), "
            "error_message = %s WHERE id = %s",
            (COMMAND_TTL_FAILURE_REASON, cmd.id),
        )
        results = _results(db, notifier)
        received_at = datetime.now(UTC)
        first = await results.handle_rep(locker.id, rid, RepResult.SUCCESS, received_at=received_at)
        second = await results.handle_rep(locker.id, rid, RepResult.SUCCESS, received_at=received_at)
        assert first == second == RelayMessageOutcome.STALE_REP

        current = await CommandRepository(db).get_by_id(cmd.id)
        assert current.status == "failed"
        assert current.executed_at is None
        assert current.error_message == COMMAND_TTL_FAILURE_REASON

        history = _outbox(locker.id, CommandHistoryProducer.CHANNEL)
        assert [p["status"] for p in history if p["status"] == "executed"] == []
        events = _outbox(locker.id, CommandLateExecutionProducer.CHANNEL)
        assert len(events) == 2
        assert {e["event_type"] for e in events} == {"command_late_execution"}
        assert events[0]["event_uid"] == events[1]["event_uid"]
        assert events[0]["metadata"]["late_kind"] == "after_failure"
        assert events[0]["metadata"]["command_id"] == cmd.id
        assert len(notifier.created) == 1
        assert notifier.created[0]["tags"]["event_uid"] == events[0]["event_uid"]
        assert f"RELAY-LATE-{suffix}" in notifier.created[0]["text"]

    try:
        asyncio.run(scenario())
    finally:
        if created_id is not None:
            _cleanup_locker(created_id)


def test_duplicate_success_rep_is_ignored(db):
    suffix = uuid.uuid4().hex[:8]
    created_id: str | None = None
    notifier = _RecordingNotifier()

    async def scenario():
        nonlocal created_id
        locker, cmd, rid = await _relay_command(db, suffix, 4500000)
        created_id = locker.id
        _exec_sql("UPDATE locker_commands SET status = 'sent', sent_at = now() WHERE id = %s",
                  (cmd.id,))
        results = _results(db, notifier)
        assert await results.handle_rep(locker.id, rid, RepResult.SUCCESS) == \
            RelayMessageOutcome.PROCESSED
        executed = await CommandRepository(db).get_by_id(cmd.id)
        assert executed.status == "executed"
        outbox_before = {
            ch: len(_outbox(locker.id, ch))
            for ch in (CommandHistoryProducer.CHANNEL, CommandLateExecutionProducer.CHANNEL)
        }
        notified_before = len(notifier.created)

        assert await results.handle_rep(locker.id, rid, RepResult.SUCCESS) == \
            RelayMessageOutcome.STALE_REP

        after = await CommandRepository(db).get_by_id(cmd.id)
        assert after.executed_at == executed.executed_at
        assert after.result == executed.result
        for ch, n in outbox_before.items():
            assert len(_outbox(locker.id, ch)) == n
        assert len(notifier.created) == notified_before

    try:
        asyncio.run(scenario())
    finally:
        if created_id is not None:
            _cleanup_locker(created_id)


def test_replay_success_long_after_executed_emits_replay_event(db):
    suffix = uuid.uuid4().hex[:8]
    created_id: str | None = None
    notifier = _RecordingNotifier()

    async def scenario():
        nonlocal created_id
        locker, cmd, rid = await _relay_command(db, suffix, 4600000)
        created_id = locker.id
        _exec_sql(
            "UPDATE locker_commands SET status = 'executed', "
            "executed_at = now() - interval '1 day', result = '{\"ok\": true}'::jsonb "
            "WHERE id = %s",
            (cmd.id,),
        )
        results = _results(db, notifier)
        outcome = await results.handle_rep(locker.id, rid, RepResult.SUCCESS)
        assert outcome == RelayMessageOutcome.STALE_REP
        events = _outbox(locker.id, CommandLateExecutionProducer.CHANNEL)
        assert len(events) == 1
        assert events[0]["event_type"] == "command_late_execution"
        assert events[0]["severity"] == "high"
        assert events[0]["metadata"]["late_kind"] == "replay"
        assert len(notifier.created) == 1
        current = await CommandRepository(db).get_by_id(cmd.id)
        assert current.result == {"ok": True}

    try:
        asyncio.run(scenario())
    finally:
        if created_id is not None:
            _cleanup_locker(created_id)
