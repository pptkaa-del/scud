import asyncio
import contextlib

from app.mqtt.offline_monitor import (
    OfflineMonitor,
    sql_mark_stale_offline,
)


def test_mark_stale_offline_sql_is_single_guarded_update():
    upd = sql_mark_stale_offline()
    # Один атомарный UPDATE … RETURNING (никакого отдельного SELECT-кандидатов).
    assert upd.startswith("UPDATE lockers SET connectivity = 'offline' WHERE ")
    assert "transport = 'mqtt'" in upd
    assert "connectivity = 'online'" in upd
    assert "now() - make_interval(secs => %s)" in upd
    assert "last_seen_at <" in upd
    assert "RETURNING id" in upd
    # Ровно один bind-параметр — порог в секундах.
    assert upd.count("%s") == 1


class FakeDB:
    """Single-statement offline sweep double: один write-UPDATE … RETURNING id
    (read_only=False) возвращает id'шники реально флипнутых шкафов.

    ``flipped_ids`` — то, что вернёт батч-UPDATE: WHERE уже отфильтровал
    свежие/уже-offline шкафы на стороне БД, так что наружу приходят только
    подтверждённые переходы online→offline."""

    def __init__(self, flipped_ids):
        self.calls = []
        self._flipped_ids = list(flipped_ids)
        self._served = False

    async def fetch_all(self, sql, params=None, *, read_only=True):
        self.calls.append({"sql": sql, "params": params, "read_only": read_only})
        if self._served:
            return []
        self._served = True
        return [{"id": lid} for lid in self._flipped_ids]


class FakeQueue:
    def __init__(self):
        self.unregistered = []

    async def unregister_connection(self, locker_id, transport=None):
        self.unregistered.append(locker_id)


def test_sweep_once_marks_offline_and_unregisters():
    async def scenario():
        db = FakeDB(flipped_ids=["L1", "L2"])
        queue = FakeQueue()
        monitor = OfflineMonitor(db, queue, timeout_s=350, interval_s=0.01)

        marked = await monitor._sweep_once()

        assert marked == ["L1", "L2"]
        # Ровно один батч-UPDATE: write-side (read_only=False) с порогом в params.
        assert len(db.calls) == 1
        assert db.calls[0]["params"] == (350,)
        assert db.calls[0]["read_only"] is False
        assert "RETURNING id" in db.calls[0]["sql"]
        # Both stale lockers were torn down in the command queue.
        assert queue.unregistered == ["L1", "L2"]

    asyncio.run(scenario())


def test_sweep_once_only_touches_returned_ids():
    """Свежий/уже-offline шкаф отфильтрован WHERE на стороне БД и НЕ попадает в
    RETURNING — значит вызывающий его не трогает (ни unregister, ни событие).

    Здесь FakeDB возвращает только L2 (как реальный батч-UPDATE, чей WHERE
    исключил всё остальное): монитор снимает с транспорта и эмитит ровно его."""
    async def scenario():
        db = FakeDB(flipped_ids=["L2"])
        queue = FakeQueue()
        monitor = OfflineMonitor(db, queue, timeout_s=350, interval_s=0.01)

        marked = await monitor._sweep_once()

        assert marked == ["L2"]
        assert queue.unregistered == ["L2"]

    asyncio.run(scenario())


def test_sweep_once_no_stale_rows_is_noop():
    async def scenario():
        db = FakeDB(flipped_ids=[])
        queue = FakeQueue()
        monitor = OfflineMonitor(db, queue, timeout_s=350, interval_s=0.01)
        marked = await monitor._sweep_once()
        assert marked == []
        # Батч-UPDATE всё равно выполняется (он же атомарный SELECT+write), но
        # ничего не флипнул → транспорт никого не снимаем.
        assert len(db.calls) == 1
        assert queue.unregistered == []
    asyncio.run(scenario())


def test_monitor_loop_runs_sweep_then_stops():
    async def scenario():
        db = FakeDB(flipped_ids=["L1"])
        queue = FakeQueue()
        monitor = OfflineMonitor(db, queue, timeout_s=350, interval_s=0.01)

        task = asyncio.create_task(monitor.start())
        await asyncio.sleep(0.05)
        monitor.stop()
        await asyncio.sleep(0.02)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert "L1" in queue.unregistered

    asyncio.run(scenario())
