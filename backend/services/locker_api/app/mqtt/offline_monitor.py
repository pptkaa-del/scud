"""Offline detection: a relay is offline if no stat for
MQTT_OFFLINE_TIMEOUT_S (default 350s). stat=0 handling is done inline in ingest."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

OFFLINE_TIMEOUT_S = int(os.getenv("MQTT_OFFLINE_TIMEOUT_S", "350"))
CHECK_INTERVAL_S = float(os.getenv("MQTT_OFFLINE_CHECK_INTERVAL_S", "30"))


def sql_mark_stale_offline() -> str:
    """Один атомарный UPDATE … RETURNING вместо SELECT-кандидатов + per-id цикла.

    WHERE перепроверяет «протухший mqtt-шкаф» в момент записи, поэтому
    TOCTOU-безопасен по построению: шкаф, успевший вернуться online (свежий
    stat → connectivity=online + бамп last_seen_at), уже не матчит предикат и НЕ
    флипается. RETURNING id отдаёт ровно те шкафы, что реально перешли в offline,
    — их и только их вызывающий снимает с транспорта + эмитит offline-событие.
    Единственный %s — порог в секундах. transport='mqtt' оставляет WS-шкафы
    нетронутыми (у них свой heartbeat-путь).
    """
    return (
        "UPDATE lockers SET connectivity = 'offline' "
        "WHERE transport = 'mqtt' AND connectivity = 'online' "
        "AND (last_seen_at IS NULL OR last_seen_at < now() - make_interval(secs => %s)) "
        "RETURNING id"
    )


class OfflineMonitor:
    def __init__(self, db: Any, command_queue: Any,
                 timeout_s: int = OFFLINE_TIMEOUT_S, interval_s: float = CHECK_INTERVAL_S,
                 offline_producer: Any = None) -> None:
        self._db = db
        self._queue = command_queue
        self._timeout_s = timeout_s
        self._interval_s = interval_s
        self._running = False
        # OfflineEventProducer: эмитит offline history event при флипе шкафа
        # offline. None — событие не пишется (некоторые unit-тесты).
        self._offline = offline_producer

    async def start(self) -> None:
        self._running = True
        logger.info("Relay offline monitor started (timeout=%ss)", self._timeout_s)
        while self._running:
            try:
                await self._sweep_once()
            except Exception:
                logger.exception("offline monitor query failed")
            await asyncio.sleep(self._interval_s)

    async def _sweep_once(self) -> list[str]:
        """Помечает протухшие mqtt-шкафы offline и снимает их транспорт.

        Один write-UPDATE … RETURNING (read_only=False) флипает все протухшие
        шкафы атомарно и возвращает их id — без N+1 (SELECT-кандидатов + per-id
        guard убраны). WHERE перепроверяет stale-предикат в момент записи, так
        что шкаф, вернувшийся online до коммита, не флипается и в RETURNING не
        попадёт (TOCTOU-safe). Для каждого реально помеченного id снимаем
        транспорт и эмитим offline-событие.
        """
        rows = await self._db.fetch_all(
            sql_mark_stale_offline(), (self._timeout_s,), read_only=False,
        )
        flipped = [str(row["id"]) for row in rows or []]
        for locker_id in flipped:
            await self._queue.unregister_connection(locker_id)
            await self._emit_offline(locker_id)
            logger.info("relay locker %s marked offline (stale stat)", locker_id)
        return flipped

    async def _emit_offline(self, locker_id: str) -> None:
        """Поставить offline history event в outbound queue.

        Reason heartbeat_timeout: шкаф протух по отсутствию свежего stat —
        прямой аналог heartbeat-таймаута WS-пути."""
        if self._offline is None:
            return
        try:
            from ..services.history_producers.offline_producer import (
                REASON_HEARTBEAT_TIMEOUT,
            )
            await self._offline.publish_offline(
                locker_id=locker_id, reason=REASON_HEARTBEAT_TIMEOUT)
        except Exception as exc:
            logger.error("Failed to enqueue OFFLINE event for locker %s: %s",
                         locker_id, exc)

    def stop(self) -> None:
        self._running = False
