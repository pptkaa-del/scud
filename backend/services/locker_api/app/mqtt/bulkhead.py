"""Ограничить параллельный ingest MQTT-сообщений, чтобы шторм реле не захватил пул БД.

gmqtt диспатчит каждое сообщение как отдельный detached-таск (ensure_future),
поэтому шторм connect/stat от тысяч реле планирует тысячи одновременных
handle_message, каждый из которых веером обращается к ~9 операциям в БД —
общий пул из 20 соединений оказывается полностью занят, и /health + доставка
команд голодают.

IngestBulkhead ограничивает параллелизм (семафор) и общее число находящихся
в работе + ожидающих вызовов stat/rep. Сверх лимита loss-tolerant сообщения
отбрасываются (вызывающий фиксирует DROPPED_OVERLOAD); rep (ответы на команды)
освобождены от проброса и всегда исполняются.

Кадры photos идут отдельной полосой ByteBudgetBulkhead: свой семафор и бюджет
ожидающих байт, поэтому серия фото по ~10 МиБ не занимает слоты stat/rep и не
раздувает память неограниченной очередью.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class IngestBulkhead:
    def __init__(self, max_concurrency: int, max_pending: int) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0")
        if max_pending < max_concurrency:
            raise ValueError("max_pending must be >= max_concurrency")
        self._sem = asyncio.Semaphore(max_concurrency)
        self._max_pending = max_pending
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def run(
        self, is_exempt: bool, handler: Callable[[], Awaitable[None]]
    ) -> bool:
        """Запустить handler под защитой bulkhead. Возвращает False при сбросе.

        Не-exempt вызовы отбрасываются, если in_flight достиг max_pending.
        Exempt-вызовы (rep) всегда допускаются: rep приходит 1:1 на выданную
        команду, поэтому его поток ограничен темпом выдачи команд (низкий) и не
        может сам по себе исчерпать пул — в отличие от stat-шторма. Счётчик
        in_flight покрывает как ожидающие семафора, так и уже выполняющиеся
        хендлеры (однопоточный asyncio — проверка-инкремент атомарна).
        """
        if not is_exempt and self._in_flight >= self._max_pending:
            return False
        self._in_flight += 1
        try:
            async with self._sem:
                await handler()
        finally:
            self._in_flight -= 1
        return True


class ByteBudgetBulkhead:
    def __init__(self, max_concurrency: int, max_pending_bytes: int) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0")
        if max_pending_bytes <= 0:
            raise ValueError("max_pending_bytes must be > 0")
        self._sem = asyncio.Semaphore(max_concurrency)
        self._max_pending_bytes = max_pending_bytes
        self._pending_bytes = 0

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    async def run(self, cost_bytes: int, handler: Callable[[], Awaitable[None]]) -> bool:
        """Запустить handler, если ожидающие + выполняемые укладываются в бюджет байт."""
        if self._pending_bytes + cost_bytes > self._max_pending_bytes:
            return False
        self._pending_bytes += cost_bytes
        try:
            async with self._sem:
                await handler()
        finally:
            self._pending_bytes -= cost_bytes
        return True
