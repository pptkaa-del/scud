"""Общие фикстуры для unit-тестов Notification API

ВАЖНО: AsyncHTTPTestCase от Tornado не совместим с механизмом фикстур pytest.
Для таких тестов моки создаются напрямую в методе get_app() классов тестов.
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.notification_data.db.models.notification import NotificationType


@pytest.fixture
def db_mock():
    mock = MagicMock()
    mock.fetch_one = AsyncMock()
    mock.fetch_one_strict = AsyncMock()
    mock.fetch_all = AsyncMock()
    mock.execute = AsyncMock()
    return mock


@pytest.fixture
def test_notification_row():
    return {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "type": NotificationType.PERSISTENT,
        "text": "Тестовое уведомление",
        "tags": {"severity": "high"},
        "start_at": datetime.now(),
        "is_cancelled": False,
        "created_at": datetime.now(),
        "created_by": 1,
    }


@pytest.fixture
def test_recipient_row():
    return {
        "id": 1,
        "notification_id": "550e8400-e29b-41d4-a716-446655440000",
        "user_id": None,
        "role": "operator",
    }
