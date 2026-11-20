"""Helpers для чтения docker-логов backend-сервисов из E2E-тестов.

Используется для проверки появления специфических WARN-сообщений в логах
locker_api/notification_api и т.п. Требует mount'а ``/var/run/docker.sock``
в test-контейнер (см. ``tests/system/docker-compose.yaml``).
"""
from __future__ import annotations

import logging
from typing import Iterable

import docker

logger = logging.getLogger(__name__)

# Имена контейнеров стека (как поднимает ``backend/docker-compose.yaml``).
LOCKER_API_CONTAINER = "backend-locker_api-1"
NOTIFICATION_API_CONTAINER = "backend-notification_api-1"


def get_service_logs(container_name: str, since_seconds: int = 60) -> list[str]:
    """Вернуть строки docker-логов указанного контейнера за последние N секунд.

    Если контейнер не найден или docker-сокет недоступен — возвращает пустой
    список и логирует warning (не падаем — это диагностический хелпер).
    """
    try:
        client = docker.from_env()
        container = client.containers.get(container_name)
        raw = container.logs(since=since_seconds, stream=False)
    except Exception as exc:
        logger.warning("Failed to read logs for %s: %s", container_name, exc)
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return raw.splitlines()


def get_locker_api_logs(since_seconds: int = 60) -> list[str]:
    """Шорткат для ``backend-locker_api-1``."""
    return get_service_logs(LOCKER_API_CONTAINER, since_seconds=since_seconds)


def grep_logs(lines: Iterable[str], *patterns: str) -> list[str]:
    """Substring-match: вернуть строки, содержащие ВСЕ patterns (AND).

    Полезно для специфичной фильтрации по полям WARN-сообщений (sensor_id +
    expected_type=boolean одновременно).
    """
    result = []
    for line in lines:
        if all(p in line for p in patterns):
            result.append(line)
    return result
