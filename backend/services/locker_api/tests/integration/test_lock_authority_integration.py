"""Real-DB интеграционные тесты действующей команды замка (протокол платы §7.1).

CommandRepository.is_open_authorized и SensorRepository.swap_value проверяются на
реальных строках: порядок публикации (sent_at), отвергнутые платой команды,
неопубликованные команды, изоляция шкафов и атомарность перехода значения датчика.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid

import psycopg2

from app.locker_data.db.repositories.command import CommandRepository
from app.locker_data.db.repositories.locker import LockerRepository
from app.locker_data.db.repositories.sensor import SensorRepository
from app.mqtt.results import DEVICE_REJECTION_REASONS
from shared.database import AsyncPostgres, PostgresConfig


def _db_config() -> PostgresConfig:
    return PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        database=os.getenv("DB_NAME", "scud"),
    )


def _conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud"),
    )


def _run(coro_fn):
    async def run():
        db = AsyncPostgres(_db_config())
        try:
            return await coro_fn(db)
        finally:
            db.close()
    return asyncio.run(run())


class _Locker:
    def __enter__(self):
        self.conn = _conn()
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO lockers (inventory_number) VALUES (%s) RETURNING id",
                        (f"AUTH-INT-{uuid.uuid4().hex[:8]}",))
            self.id = str(cur.fetchone()[0])
        self.conn.commit()
        return self

    def __exit__(self, *exc):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM lockers WHERE id = %s", (self.id,))
        self.conn.commit()
        self.conn.close()

    def command(self, command_type: str, status: str, sent_ago_s: float | None,
                error_message: str | None = None) -> None:
        """Команда, опубликованная sent_ago_s секунд назад (None — не публиковалась)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO locker_commands (locker_id, type, status, sent_at, error_message)
                VALUES (%s, %s, %s,
                        CASE WHEN %s::float8 IS NULL THEN NULL
                             ELSE now() - make_interval(secs => %s::float8) END,
                        %s)
                """,
                (self.id, command_type, status, sent_ago_s, sent_ago_s, error_message),
            )
        self.conn.commit()

    def authorized(self, at: datetime | None = None) -> bool:
        at = at or datetime.now(UTC)
        return _run(lambda db: CommandRepository(db).is_open_authorized(
            self.id, at, DEVICE_REJECTION_REASONS))


def test_no_published_lock_command_means_closed():
    with _Locker() as locker:
        assert locker.authorized() is False
        locker.command("open", "pending", None)
        assert locker.authorized() is False


def test_last_published_lock_command_decides():
    with _Locker() as locker:
        locker.command("close", "executed", 600)
        locker.command("open", "executed", 300)
        assert locker.authorized() is True
        locker.command("close", "executed", 100)
        assert locker.authorized() is False


def test_publish_order_beats_creation_order():
    """Команда, созданная раньше, но опубликованная позже, — действующая."""
    with _Locker() as locker:
        locker.command("open", "executed", 10)
        locker.command("close", "executed", 20)
        assert locker.authorized() is True


def test_unacknowledged_or_ttl_failed_open_counts():
    """rep не пришёл или потерян: OPEN остаётся в retained com — плата её выполнит."""
    for status, error in (("sent", None), ("failed", "истёк срок ожидания ответа шкафа"),
                          ("failed", None)):
        with _Locker() as locker:
            locker.command("close", "executed", 600)
            locker.command("open", status, 5, error)
            assert locker.authorized() is True, (status, error)


def test_open_rejected_by_board_does_not_count():
    for reason in DEVICE_REJECTION_REASONS:
        with _Locker() as locker:
            locker.command("close", "executed", 600)
            locker.command("open", "failed", 5, reason)
            assert locker.authorized() is False, reason


def test_command_published_after_stat_does_not_count():
    with _Locker() as locker:
        locker.command("close", "executed", 600)
        locker.command("open", "sent", 1)
        assert locker.authorized(datetime.now(UTC) - timedelta(seconds=10)) is False


def test_non_lock_commands_are_ignored():
    with _Locker() as locker:
        locker.command("open", "executed", 600)
        locker.command("reboot", "executed", 300)
        assert locker.authorized() is True


def test_other_locker_commands_are_isolated():
    with _Locker() as target, _Locker() as other:
        other.command("open", "executed", 10)
        assert target.authorized() is False


def test_swap_value_reports_previous_and_change_once():
    with _Locker() as locker:
        async def scenario(db):
            sensors = SensorRepository(db)
            sid = await sensors.get_or_create_id_by_type_code(locker.id, "door", "relay-door")
            first = await sensors.swap_value(sid, False)
            same = await sensors.swap_value(sid, False)
            concurrent = await asyncio.gather(*(sensors.swap_value(sid, True) for _ in range(5)))
            return first, same, concurrent

        first, same, concurrent = _run(scenario)
        assert first == (True, None)
        assert same == (False, None)
        assert sorted(concurrent, key=lambda r: r[0]) == [(False, None)] * 4 + [(True, False)]


def test_late_device_rejection_revokes_ttl_failed_open():
    with _Locker() as locker:
        locker.command("close", "executed", 600)
        locker.command("open", "failed", 5, "истёк срок ожидания ответа шкафа")
        assert locker.authorized() is True
        with locker.conn.cursor() as cur:
            cur.execute("SELECT id FROM locker_commands WHERE locker_id = %s AND type = 'open'",
                        (locker.id,))
            command_id = str(cur.fetchone()[0])

        def mark(db):
            return CommandRepository(db).mark_device_rejected(
                command_id, locker.id, "failed", DEVICE_REJECTION_REASONS)

        assert _run(mark) is True
        assert _run(mark) is False
        assert locker.authorized() is False


def test_device_rejection_does_not_touch_executed_or_foreign_command():
    with _Locker() as locker, _Locker() as other:
        locker.command("open", "executed", 5)
        with locker.conn.cursor() as cur:
            cur.execute("SELECT id FROM locker_commands WHERE locker_id = %s", (locker.id,))
            command_id = str(cur.fetchone()[0])
        for target in (locker.id, other.id):
            assert _run(lambda db, t=target: CommandRepository(db).mark_device_rejected(
                command_id, t, "failed", DEVICE_REJECTION_REASONS)) is False
        assert locker.authorized() is True


def _status(locker) -> str:
    with locker.conn.cursor() as cur:
        cur.execute("SELECT operational_status FROM lockers WHERE id = %s", (locker.id,))
        return str(cur.fetchone()[0])


def test_raise_alarm_only_from_normal_and_once():
    with _Locker() as locker:
        async def concurrent(db):
            repo = LockerRepository(db)
            return await asyncio.gather(*(repo.raise_alarm(locker.id) for _ in range(5)))

        results = _run(concurrent)
        assert sum(r is not None for r in results) == 1
        assert _status(locker) == "alarm"
        assert _run(lambda db: LockerRepository(db).raise_alarm(locker.id)) is None


def test_raise_alarm_skips_maintenance():
    with _Locker() as locker:
        with locker.conn.cursor() as cur:
            cur.execute("UPDATE lockers SET operational_status = 'maintenance' WHERE id = %s",
                        (locker.id,))
        locker.conn.commit()
        assert _run(lambda db: LockerRepository(db).raise_alarm(locker.id)) is None
        assert _status(locker) == "maintenance"
