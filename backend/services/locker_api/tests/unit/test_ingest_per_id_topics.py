"""Приём телеметрии в per-id топиках (ПИВ v2) наравне с общими.

Прошивка публикует либо в `stat`, либо в `stat/{id}`. Обе схемы должны
доезжать до одного обработчика, а несовпадение id в топике и в кадре —
отбрасываться: это единственный признак публикации от чужого имени.
"""
import asyncio

from app.locker_data.db.models.locker import Connectivity

from .test_ingest import _build_ingest, _u32


def _stat_v2(relay_id: int) -> bytes:
    return (_u32(relay_id) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1)
            + _u32(1) + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))


def test_stat_in_per_id_topic_is_accepted():
    async def scenario():
        ingest, repo, queue, _ = _build_ingest({5: "L5"})
        await ingest.handle_message(topic="stat/5", payload=_stat_v2(5))
        assert "L5" in queue.registered
        assert ("L5", Connectivity.ONLINE) in repo.connectivity
    asyncio.run(scenario())


def test_stat_in_shared_topic_still_accepted():
    async def scenario():
        ingest, repo, queue, _ = _build_ingest({5: "L5"})
        await ingest.handle_message(topic="stat", payload=_stat_v2(5))
        assert ("L5", Connectivity.ONLINE) in repo.connectivity
    asyncio.run(scenario())


def test_topic_id_mismatching_frame_id_is_dropped():
    """Плата 9 не вправе публиковать телеметрию платы 5."""
    async def scenario():
        ingest, repo, queue, _ = _build_ingest({5: "L5", 9: "L9"})
        await ingest.handle_message(topic="stat/9", payload=_stat_v2(5))
        assert repo.connectivity == []
        assert queue.registered == {} or "L5" not in queue.registered
    asyncio.run(scenario())


def test_unknown_topic_touches_nothing():
    async def scenario():
        ingest, repo, queue, _ = _build_ingest({5: "L5"})
        await ingest.handle_message(topic="telemetry/5", payload=_stat_v2(5))
        assert repo.connectivity == []
    asyncio.run(scenario())
