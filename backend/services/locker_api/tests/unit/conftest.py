"""Общие фикстуры и моки для unit-тестов

ВАЖНО: AsyncHTTPTestCase от Tornado не совместим с механизмом фикстур pytest.
Для таких тестов моки создаются напрямую в методе get_app() классов тестов.

Фикстуры ниже оставлены для справки и могут использоваться в обычных pytest-тестах
(не использующих AsyncHTTPTestCase).
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from tornado.web import Application

from app.locker_data.db.models.locker import Connectivity, DoorState, OperationalStatus
from app.main import make_app


@pytest.fixture
def db_mock():
    """Создает мок базы данных с основными методами

    Примечание: Не используется в AsyncHTTPTestCase тестах.
    Для AsyncHTTPTestCase создавайте моки напрямую в get_app():

        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
    """
    mock = MagicMock()
    mock.fetch_one = AsyncMock()
    mock.fetch_one_strict = AsyncMock()
    mock.fetch_all = AsyncMock()
    mock.execute = AsyncMock()
    return mock


@pytest.fixture
def command_queue_mock():
    """Создает мок Command Queue для тестов"""
    mock = MagicMock()
    mock.register_connection = AsyncMock()
    mock.unregister_connection = AsyncMock()
    mock.is_connected = MagicMock(return_value=False)
    mock.send_command = AsyncMock()
    return mock


@pytest.fixture
def test_app(db_mock, command_queue_mock) -> Application:
    """Создает тестовое приложение Tornado с моком БД

    Примечание: Не используется в AsyncHTTPTestCase тестах.
    """
    return make_app(db_mock, command_queue_mock)


@pytest.fixture
def test_locker_row():
    """Возвращает данные тестового шкафа"""
    return {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "inventory_number": "СО №777",
        "location_address": "ул. Тестовая, 1",
        "location_lat": 55.751244,
        "location_lon": 37.618423,
        "public_key": None,
        "created_at": datetime.now(),
        "created_by": 1,
        "updated_at": datetime.now(),
        "updated_by": 1,
        "connectivity": Connectivity.OFFLINE,
        "door": DoorState.CLOSED,
        "operational_status": OperationalStatus.NORMAL,
        "last_seen_at": None,
        "last_opened_at": None,
        "description": "Тестовый шкаф",
        "metadata": {}
    }


@pytest.fixture
def test_sensor_row():
    """Возвращает данные тестового датчика"""
    return {
        "id": "660e8400-e29b-41d4-a716-446655440001",
        "locker_id": "550e8400-e29b-41d4-a716-446655440000",
        "serial_number": "TEMP-001",
        "type": "temperature",
        "current_value": 25.5,
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
        "is_active": True,
        "metadata": {}
    }
