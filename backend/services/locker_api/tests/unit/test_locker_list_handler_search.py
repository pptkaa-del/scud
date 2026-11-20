"""Unit-тесты LockerListHandler: парсинг ?q= → trimmed/None → передача в repo.

Покрывает ветки нормализации:
- q отсутствует              → repo вызывается с q=None.
- q="" / "   "               → repo вызывается с q=None (пустое значение игнорируем).
- q=" abc "                  → repo вызывается с q="abc" (trim).
- len(q.strip()) > 100       → 400 ProblemDetails, repo НЕ вызывается.
"""

from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.locker_data.db.models.locker import (
    Connectivity,
    DoorState,
    OperationalStatus,
)
from app.main import make_app


def _make_locker_row() -> dict:
    return {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "inventory_number": "СО №777",
        "location_address": "ул. Тестовая, 1",
        "location_lat": None,
        "location_lon": None,
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
        "who_open_last": None,
        "description": None,
        "metadata": {},
    }


class TestLockerListHandlerQ(AsyncHTTPTestCase):
    """Проверяет нормализацию q на уровне handler. Repo замокан, ассерт по аргументам."""

    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock(return_value={"count": 0})
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock(return_value=[])
        self.db_mock.execute = AsyncMock()

        self.cq_mock = MagicMock()
        self.cq_mock.register_connection = AsyncMock()
        self.cq_mock.unregister_connection = AsyncMock()
        self.cq_mock.is_connected = MagicMock(return_value=False)
        self.cq_mock.send_command = AsyncMock()

        return make_app(self.db_mock, self.cq_mock)

    def _last_q_get_all(self) -> object:
        """Достать q-kwarg из последнего вызова fetch_all (get_all)."""
        # get_all/count проходят через одни и те же fetch_all/fetch_one; мы
        # не можем напрямую интроспектировать kwargs репозитория, поэтому
        # ассертим по содержимому SQL и параметров.
        return self.db_mock.fetch_all.call_args

    def test_q_absent_no_ilike_in_sql(self):
        """?q отсутствует → SQL get_all не содержит ILIKE."""
        response = self.fetch("/api/v1/lockers", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        sql = self.db_mock.fetch_all.call_args[0][0]
        self.assertNotIn("ILIKE", sql)

    def test_q_empty_string_treated_as_absent(self):
        """?q= (пусто) → SQL не содержит ILIKE."""
        response = self.fetch("/api/v1/lockers?q=", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        sql = self.db_mock.fetch_all.call_args[0][0]
        self.assertNotIn("ILIKE", sql)

    def test_q_whitespace_treated_as_absent(self):
        """?q=   → после trim пусто → SQL не содержит ILIKE."""
        response = self.fetch("/api/v1/lockers?q=%20%20%20", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        sql = self.db_mock.fetch_all.call_args[0][0]
        self.assertNotIn("ILIKE", sql)

    def test_q_trimmed_passed_to_repo(self):
        """?q=%20abc%20 → repo получает 'abc' (тримминг), ILIKE появляется в SQL."""
        response = self.fetch("/api/v1/lockers?q=%20abc%20", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        sql = self.db_mock.fetch_all.call_args[0][0]
        params = self.db_mock.fetch_all.call_args[0][1]
        self.assertIn("ILIKE", sql)
        # `%abc%` — экранированных метасимволов нет, поэтому wrap прямой.
        self.assertEqual(params.count("%abc%"), 2)

    def test_q_too_long_returns_400(self):
        """len(q.strip()) > 100 → 400, repo не вызывается."""
        long_q = "x" * 101
        response = self.fetch(f"/api/v1/lockers?q={long_q}", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["status"], 400)
        self.assertIn("q", data["detail"])
        # Ни fetch_all, ни fetch_one (count) не вызваны.
        self.assertEqual(self.db_mock.fetch_all.call_count, 0)
        self.assertEqual(self.db_mock.fetch_one.call_count, 0)

    def test_q_exactly_100_chars_ok(self):
        """Длина ровно 100 — на границе, допускается."""
        q = "x" * 100
        response = self.fetch(f"/api/v1/lockers?q={q}", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        sql = self.db_mock.fetch_all.call_args[0][0]
        self.assertIn("ILIKE", sql)
