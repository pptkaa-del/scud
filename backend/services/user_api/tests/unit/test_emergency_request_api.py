"""Unit-тесты для API заявок на экстренное открытие шкафов"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.user_data.db.models.emergency_request import EmergencyRequestStatus
from app.user_data.db.models.user import UserState

LOCKER_UUID_1 = "550e8400-e29b-41d4-a716-446655440001"
LOCKER_UUID_2 = "550e8400-e29b-41d4-a716-446655440002"


class TestEmergencyRequestApi(AsyncHTTPTestCase):
    def get_app(self):
        # Создаем мок БД напрямую (AsyncHTTPTestCase не совместим с pytest fixtures)
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        return make_app(self.db_mock)

    def setUp(self):
        super().setUp()
        # Reset mocks before each test
        self.db_mock.fetch_one.reset_mock()
        self.db_mock.fetch_all.reset_mock()
        self.db_mock.execute.reset_mock()

        self.auth_user_row = {
            "id": 1, "phone": "+79123456789", "full_name": "Admin", "contractor_id": None,
            "contractor_name": None,
            "created_at": datetime.now(), "created_by": None, "updated_at": datetime.now(),
            "updated_by": None, "state": UserState.ACTIVE.value, "role": "administrator"
        }

    def test_create_emergency_request_success(self):
        """Тест: успешное создание заявки"""
        created_request = {
            "id": 1, "user_id": 1, "locker_id": LOCKER_UUID_1, "status": EmergencyRequestStatus.PENDING.value,
            "created_at": datetime.now(), "closed_at": None, "closed_by": None
        }

        detailed_request = {
            "id": 1, "user_id": 1, "user_full_name": "Admin", "user_phone": "+79123456789",
            "locker_id": LOCKER_UUID_1, "status": EmergencyRequestStatus.PENDING.value,
            "created_at": datetime.now(), "closed_at": None, "closed_by": None,
            "closed_by_full_name": None
        }

        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth
            detailed_request,    # get_by_id for details (после INSERT)
        ]
        self.db_mock.fetch_one_strict.return_value = created_request  # INSERT RETURNING *

        self.db_mock.execute.return_value = None  # log action

        body = {"locker_id": LOCKER_UUID_1}
        response = self.fetch("/api/v1/emergency-requests", method="POST",
                             body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["locker_id"], LOCKER_UUID_1)
        self.assertEqual(data["status"], EmergencyRequestStatus.PENDING.value)

    def test_create_emergency_request_missing_locker_id(self):
        """Тест: создание заявки без locker_id"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {}
        response = self.fetch("/api/v1/emergency-requests", method="POST",
                             body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertEqual(data["status"], 400)
        self.assertIn("locker_id", data["detail"])

    def test_create_emergency_request_invalid_locker_id(self):
        """Тест: создание заявки с невалидным locker_id (не UUID)"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        # Строка, не являющаяся UUID
        body = {"locker_id": "not-a-uuid"}
        response = self.fetch("/api/v1/emergency-requests", method="POST",
                             body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertEqual(data["status"], 400)
        self.assertIn("locker_id", data["detail"])

    def test_get_emergency_requests_list_success(self):
        """Тест: получение списка заявок"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth
            {"count": 2}         # count query
        ]
        self.db_mock.fetch_all.return_value = [
            {
                "id": 1, "user_id": 1, "user_full_name": "Admin", "user_phone": "+79123456789",
                "locker_id": LOCKER_UUID_1, "status": EmergencyRequestStatus.PENDING.value,
                "created_at": datetime.now(), "closed_at": None, "closed_by": None,
                "closed_by_full_name": None
            },
            {
                "id": 2, "user_id": 1, "user_full_name": "Admin", "user_phone": "+79123456789",
                "locker_id": LOCKER_UUID_2, "status": EmergencyRequestStatus.CLOSED.value,
                "created_at": datetime.now(), "closed_at": datetime.now(), "closed_by": 1,
                "closed_by_full_name": "Admin"
            }
        ]

        response = self.fetch("/api/v1/emergency-requests", method="GET",
                             headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertIn("items", data)
        self.assertIn("total", data)
        self.assertEqual(len(data["items"]), 2)

    def test_get_emergency_requests_with_filters(self):
        """Тест: получение списка заявок с фильтрами"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth
            {"count": 1}         # count query
        ]
        self.db_mock.fetch_all.return_value = [
            {
                "id": 1, "user_id": 1, "user_full_name": "Admin", "user_phone": "+79123456789",
                "locker_id": LOCKER_UUID_1, "status": EmergencyRequestStatus.PENDING.value,
                "created_at": datetime.now(), "closed_at": None, "closed_by": None,
                "closed_by_full_name": None
            }
        ]

        response = self.fetch(f"/api/v1/emergency-requests?status=pending&locker_id={LOCKER_UUID_1}",
                             method="GET", headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["status"], EmergencyRequestStatus.PENDING.value)

    def test_get_emergency_request_detail_success(self):
        """Тест: получение детальной информации о заявке"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth
            {  # get_by_id
                "id": 1, "user_id": 1, "user_full_name": "Admin", "user_phone": "+79123456789",
                "locker_id": LOCKER_UUID_1, "status": EmergencyRequestStatus.PENDING.value,
                "created_at": datetime.now(), "closed_at": None, "closed_by": None,
                "closed_by_full_name": None
            }
        ]

        self.db_mock.execute.return_value = None  # log view action

        response = self.fetch("/api/v1/emergency-request/1", method="GET",
                             headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["locker_id"], LOCKER_UUID_1)

    def test_get_emergency_request_not_found(self):
        """Тест: заявка не найдена"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth
            None                  # get_by_id returns None
        ]

        response = self.fetch("/api/v1/emergency-request/999", method="GET",
                             headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Not Found")
        self.assertEqual(data["status"], 404)
        self.assertIn("not found", data["detail"])

    def test_close_emergency_request_success(self):
        """Тест: успешное закрытие заявки"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth
            {"status": EmergencyRequestStatus.PENDING.value},  # check status
            {  # close request
                "id": 1, "user_id": 1, "locker_id": LOCKER_UUID_1, "status": EmergencyRequestStatus.CLOSED.value,
                "created_at": datetime.now(), "closed_at": datetime.now(), "closed_by": 1
            }
        ]

        self.db_mock.execute.return_value = None  # log action

        response = self.fetch("/api/v1/emergency-request/1", method="DELETE",
                             headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 204)

    def test_close_emergency_request_not_found(self):
        """Тест: закрытие несуществующей заявки"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth
            None,                 # check status - not found
            None                  # get_by_id for error check
        ]

        response = self.fetch("/api/v1/emergency-request/999", method="DELETE",
                             headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Not Found")
        self.assertEqual(data["status"], 404)
        self.assertIn("not found", data["detail"])

    def test_close_emergency_request_already_closed(self):
        """Тест: повторное закрытие уже закрытой заявки"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,    # auth
            {"status": EmergencyRequestStatus.CLOSED.value},  # check status - already closed
            {  # get_by_id for error check
                "id": 1, "user_id": 1, "user_full_name": "Admin", "user_phone": "+79123456789",
                "locker_id": LOCKER_UUID_1, "status": EmergencyRequestStatus.CLOSED.value,
                "created_at": datetime.now(), "closed_at": datetime.now(), "closed_by": 1,
                "closed_by_full_name": "Admin"
            }
        ]

        response = self.fetch("/api/v1/emergency-request/1", method="DELETE",
                             headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 409)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Conflict")
        self.assertEqual(data["status"], 409)
        self.assertIn("already closed", data["detail"])

    def test_get_emergency_request_logs_success(self):
        """Тест: получение логов действий с заявкой"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth
            {  # get_by_id to check existence
                "id": 1, "user_id": 1, "user_full_name": "Admin", "user_phone": "+79123456789",
                "locker_id": LOCKER_UUID_1, "status": EmergencyRequestStatus.PENDING.value,
                "created_at": datetime.now(), "closed_at": None, "closed_by": None,
                "closed_by_full_name": None
            }
        ]

        self.db_mock.fetch_all.return_value = [
            {
                "id": 1, "request_id": 1, "action": "created",
                "performed_by": 1, "performed_at": datetime.now(), "details": None
            },
            {
                "id": 2, "request_id": 1, "action": "viewed",
                "performed_by": 1, "performed_at": datetime.now(), "details": None
            }
        ]

        response = self.fetch("/api/v1/emergency-request/1/logs", method="GET",
                             headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["action"], "created")

    def test_get_emergency_request_logs_request_not_found(self):
        """Тест: получение логов для несуществующей заявки"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth
            None                  # get_by_id returns None
        ]

        response = self.fetch("/api/v1/emergency-request/999/logs", method="GET",
                             headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Not Found")
        self.assertEqual(data["status"], 404)
        self.assertIn("not found", data["detail"])

    def test_create_emergency_request_unauthorized(self):
        """Тест: создание заявки без авторизации"""
        response = self.fetch("/api/v1/emergency-requests", method="POST",
                             body=json.dumps({"locker_id": LOCKER_UUID_1}))

        self.assertEqual(response.code, 401)

    def test_get_emergency_requests_invalid_status(self):
        """Тест: получение списка с невалидным статусом"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        response = self.fetch("/api/v1/emergency-requests?status=invalid",
                             method="GET", headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertEqual(data["status"], 400)
        self.assertIn("status", data["detail"])
