"""Defense-in-depth (SEC-02): locker_api сам сверяет Допуск worker'а.

Независимо от ACM, command handler проверяет, что канонизированный
``locker_id`` присутствует в ``X-Remote-Lockers``. worker, чьего шкафа нет
в списке Допусков, получает 403 — даже если роль позволяет команду и даже
если ACM (по ошибке/обходу) пропустил запрос.
"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.locker_data.db.models.command import CommandStatus, CommandType
from app.main import make_app
import shared.enums as se

LOCKER_X = "550e8400-e29b-41d4-a716-446655440000"
LOCKER_Y = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
X_DASHLESS = "550e8400e29b41d4a716446655440000"


class TestCommandGrantCheck(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock(return_value=None)
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock(return_value=[])
        self.db_mock.execute = AsyncMock(return_value=0)

        self.command_queue_mock = MagicMock()
        self.command_queue_mock.is_connected.return_value = False
        self.command_queue_mock.send_command = AsyncMock(return_value=True)
        return make_app(self.db_mock, self.command_queue_mock)

    def setUp(self):
        super().setUp()
        self.test_locker = {
            "id": LOCKER_X,
            "inventory_number": "СО №777", "location_address": "ул. Тестовая, 1",
            "location_lat": 55.751244, "location_lon": 37.618423,
            "auth_key": "test_key", "created_at": datetime.now(),
            "created_by": 1, "updated_at": datetime.now(),
            "updated_by": 1, "connectivity": "offline", "door": "closed",
            "operational_status": "normal",
            "last_seen_at": None, "last_opened_at": None, "description": "Тест",
            "metadata": {},
        }
        self.test_command = {
            "id": "770e8400-e29b-41d4-a716-446655440002",
            "locker_id": LOCKER_X,
            "type": CommandType.OPEN, "status": CommandStatus.PENDING,
            "parameters": {}, "created_at": datetime.now(),
            "created_by": 1, "sent_at": None, "delivered_at": None,
            "executed_at": None, "failed_at": None,
            "result": {}, "error_message": None,
            "retry_count": 0, "max_retries": 3,
        }

    def _post(self, locker_id, headers):
        return self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={"Content-Type": "application/json", **headers},
            body=json.dumps({"type": "open", "parameters": {}}),
        )

    def test_command_enums_are_shared(self):
        """Модель locker_api re-export'ит enum'ы из shared.enums (единый источник)."""
        self.assertIs(CommandType, se.CommandType)
        self.assertIs(CommandStatus, se.CommandStatus)

    def test_worker_without_grant_denied(self):
        """worker без целевого шкафа в X-Remote-Lockers → 403; команда не создана."""
        self.db_mock.fetch_one.reset_mock()
        self.db_mock.execute.reset_mock()
        resp = self._post(
            LOCKER_X,
            {"X-Remote-User": "1", "X-Remote-Role": "worker",
             "X-Remote-Lockers": LOCKER_Y},
        )
        self.assertEqual(resp.code, 403)
        self.db_mock.fetch_one.assert_not_called()
        self.db_mock.execute.assert_not_called()

    def test_worker_empty_lockers_denied(self):
        """Пустой X-Remote-Lockers (нет Допусков) → 403."""
        self.db_mock.fetch_one.reset_mock()
        resp = self._post(
            LOCKER_X,
            {"X-Remote-User": "1", "X-Remote-Role": "worker",
             "X-Remote-Lockers": ""},
        )
        self.assertEqual(resp.code, 403)
        self.db_mock.fetch_one.assert_not_called()

    def test_worker_dashless_not_granted_denied(self):
        """Не-канонический (dashless) UUID не-granted шкафа → 403."""
        self.db_mock.fetch_one.reset_mock()
        # Допуск на Y, путь — dashless X: не должен совпасть.
        resp = self._post(
            X_DASHLESS,
            {"X-Remote-User": "1", "X-Remote-Role": "worker",
             "X-Remote-Lockers": LOCKER_Y},
        )
        self.assertEqual(resp.code, 403)
        self.db_mock.fetch_one.assert_not_called()

    def test_worker_with_grant_allowed(self):
        """worker с целевым шкафом в Допусках → 201."""
        self.db_mock.fetch_one.side_effect = [self.test_locker]
        self.db_mock.fetch_one_strict.return_value = self.test_command
        resp = self._post(
            LOCKER_X,
            {"X-Remote-User": "1", "X-Remote-Role": "worker",
             "X-Remote-Lockers": LOCKER_X},
        )
        self.assertEqual(resp.code, 201)

    def test_worker_dashless_path_granted_allowed(self):
        """dashless путь granted-шкафа → 201 (канонизация обеих сторон)."""
        self.db_mock.fetch_one.side_effect = [self.test_locker]
        self.db_mock.fetch_one_strict.return_value = self.test_command
        resp = self._post(
            X_DASHLESS,
            {"X-Remote-User": "1", "X-Remote-Role": "worker",
             "X-Remote-Lockers": LOCKER_X},
        )
        self.assertEqual(resp.code, 201)

    def test_admin_skips_grant_check(self):
        """administrator не ограничен X-Remote-Lockers (роль управляет)."""
        self.db_mock.fetch_one.side_effect = [self.test_locker]
        self.db_mock.fetch_one_strict.return_value = self.test_command
        resp = self._post(
            LOCKER_X,
            {"X-Remote-User": "1", "X-Remote-Role": "administrator",
             "X-Remote-Lockers": "*"},
        )
        self.assertEqual(resp.code, 201)
