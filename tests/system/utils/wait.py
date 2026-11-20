"""Polling helper для ожидания асинхронных событий в синхронных тестах."""

from __future__ import annotations

import time
from typing import Callable


def wait_for(
    condition_fn: Callable[[], bool],
    timeout: float = 30.0,
    interval: float = 0.5,
    message: str = "Condition not met",
) -> None:
    """Ожидает пока condition_fn() вернёт True.

    Args:
        condition_fn: функция без аргументов, возвращающая bool
        timeout: максимальное время ожидания в секундах
        interval: интервал опроса в секундах
        message: сообщение об ошибке при таймауте

    Raises:
        TimeoutError: если условие не выполнено в течение timeout секунд
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition_fn():
                return
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"{message} (timeout={timeout}s)")


def wait_for_value(
    getter_fn: Callable[[], object],
    expected,
    timeout: float = 30.0,
    interval: float = 0.5,
    message: str | None = None,
) -> None:
    """Ожидает пока getter_fn() вернёт expected значение.

    Args:
        getter_fn: функция без аргументов, возвращающая значение для сравнения
        expected: ожидаемое значение
        timeout: максимальное время ожидания
        interval: интервал опроса
        message: сообщение об ошибке
    """
    actual = None

    def check():
        nonlocal actual
        actual = getter_fn()
        return actual == expected

    msg = message or f"Expected {expected!r}, got {actual!r}"
    wait_for(check, timeout=timeout, interval=interval, message=msg)
