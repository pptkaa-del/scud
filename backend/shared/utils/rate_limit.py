"""
In-memory token bucket для per-connection rate-limit.

Подходит для ограничения частоты сообщений от одного устройства/клиента в
рамках одного процесса. Для распределённого rate-limit нужен Redis или
аналогичный shared store — это вне области данной утилиты.
"""

from __future__ import annotations

import time


class TokenBucket:
    """Простой token bucket: `capacity` токенов, восполняемых со скоростью `rate` токенов/сек.

    Поток токенов накапливается в `_tokens` (float). Каждый `try_consume()`
    обновляет счётчик от прошедшего времени и снимает один токен, если он есть.
    """

    __slots__ = ("_capacity", "_rate", "_tokens", "_last_refill")

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._rate = float(rate)
        self._capacity = float(capacity) if capacity is not None else float(rate)
        self._tokens = self._capacity
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def try_consume(self, amount: float = 1.0) -> bool:
        """Снять `amount` токенов. Вернёт True при успехе, False если токенов нет."""
        self._refill()
        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False

    def refund(self, amount: float = 1.0) -> None:
        """Вернуть `amount` токенов, снятых под действие, которое не состоялось."""
        self._refill()
        self._tokens = min(self._capacity, self._tokens + amount)
