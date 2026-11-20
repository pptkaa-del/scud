"""Unit-тесты для Notification API"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app


class TestNotificationApi(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        return make_app(self.db_mock)

    def setUp(self):
        super().setUp()
        self.db_mock.fetch_one.return_value = None
        self.db_mock.fetch_one.side_effect = None
        self.db_mock.fetch_all.return_value = []
        self.db_mock.fetch_all.side_effect = None
        self.db_mock.execute.return_value = 0
        self.db_mock.execute.side_effect = None

        self.test_notification = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "type": "persistent",
            "text": "Тестовое уведомление",
            "tags": {"severity": "high"},
            "start_at": datetime(2024, 1, 15, 12, 0, 0),
            "is_cancelled": False,
            "created_at": datetime(2024, 1, 15, 12, 0, 0),
            "created_by": 1,
        }

        self.test_recipient_role = {
            "id": 1,
            "notification_id": "550e8400-e29b-41d4-a716-446655440000",
            "user_id": None,
            "role": "operator",
        }

    # --- Auth tests ---

    def test_unauthorized_missing_header(self):
        """Отсутствует X-Remote-User"""
        response = self.fetch("/api/v1/notifications", method="GET")
        self.assertEqual(response.code, 401)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Unauthorized")
        self.assertIn("Missing X-Remote-User", data["detail"])

    def test_unauthorized_invalid_header(self):
        """Невалидный X-Remote-User"""
        response = self.fetch("/api/v1/notifications", method="GET",
                              headers={"X-Remote-User": "abc"})
        self.assertEqual(response.code, 401)

    # --- GET /notifications ---

    def test_get_notifications_list_empty(self):
        """Пустой список уведомлений (paginated envelope)."""
        self.db_mock.fetch_all.return_value = []
        self.db_mock.fetch_one.return_value = {"count": 0}
        response = self.fetch("/api/v1/notifications", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["limit"], 100)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["items"], [])

    def test_get_notifications_list_success(self):
        """Список с одним уведомлением (paginated envelope)."""
        self.db_mock.fetch_all.return_value = [self.test_notification]
        self.db_mock.fetch_one.return_value = {"count": 1}
        response = self.fetch("/api/v1/notifications", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["type"], "persistent")

    # --- POST /notifications ---

    def test_create_persistent_notification(self):
        """Создание persistent уведомления с ролями"""
        self.db_mock.fetch_one_strict.return_value = self.test_notification  # INSERT RETURNING *
        self.db_mock.fetch_all.return_value = [self.test_recipient_role]

        payload = {
            "type": "persistent",
            "text": "Тестовое уведомление",
            "tags": {"severity": "high"},
            "start_at": "2024-01-15T12:00:00Z",
            "roles": ["operator"],
        }
        response = self.fetch("/api/v1/notifications", method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=json.dumps(payload))
        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertIn("notification", data)
        self.assertIn("recipients", data)

    def test_create_one_time_sms_rejected(self):
        """Бывший SMS-тип one_time_sms больше не поддерживается → 400."""
        payload = {
            "type": "one_time_sms",
            "text": "Код: 123456",
            "scheduled_at": "2024-01-15T12:00:00Z",
        }
        response = self.fetch("/api/v1/notifications", method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=json.dumps(payload))
        self.assertEqual(response.code, 400)

    def test_create_periodic_sms_rejected(self):
        """Бывший SMS-тип periodic_sms больше не поддерживается → 400."""
        payload = {
            "type": "periodic_sms",
            "text": "Отчет",
            "interval_minutes": 60,
            "start_at": "2024-01-15T12:00:00Z",
        }
        response = self.fetch("/api/v1/notifications", method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=json.dumps(payload))
        self.assertEqual(response.code, 400)

    def test_create_notification_empty_text(self):
        """Ошибка: пустой text"""
        payload = {
            "type": "persistent",
            "text": "",
            "start_at": "2024-01-15T12:00:00Z",
            "roles": ["operator"],
        }
        response = self.fetch("/api/v1/notifications", method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=json.dumps(payload))
        self.assertEqual(response.code, 400)

    def test_create_persistent_missing_recipients(self):
        """Ошибка: persistent без user_ids и roles"""
        payload = {
            "type": "persistent",
            "text": "Тест",
            "start_at": "2024-01-15T12:00:00Z",
        }
        response = self.fetch("/api/v1/notifications", method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=json.dumps(payload))
        self.assertEqual(response.code, 400)

    # --- GET /notification/{id} ---

    def test_get_notification_by_id_success(self):
        """Получение уведомления по ID"""
        self.db_mock.fetch_one.return_value = self.test_notification
        self.db_mock.fetch_all.return_value = [self.test_recipient_role]

        response = self.fetch(
            "/api/v1/notification/550e8400-e29b-41d4-a716-446655440000",
            method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertIn("notification", data)
        self.assertIn("recipients", data)
        self.assertEqual(data["notification"]["text"], "Тестовое уведомление")

    def test_get_notification_by_id_not_found(self):
        """Уведомление не найдено"""
        self.db_mock.fetch_one.return_value = None
        response = self.fetch(
            "/api/v1/notification/550e8400-e29b-41d4-a716-000000000000",
            method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)

    # --- DELETE /notification/{id} ---

    def test_cancel_notification_success(self):
        """Отмена уведомления"""
        cancelled = dict(self.test_notification, is_cancelled=True)
        self.db_mock.fetch_one.return_value = cancelled

        response = self.fetch(
            "/api/v1/notification/550e8400-e29b-41d4-a716-446655440000",
            method="DELETE", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertTrue(data["is_cancelled"])

    def test_cancel_notification_not_found(self):
        """Отмена несуществующего уведомления"""
        self.db_mock.fetch_one.return_value = None
        response = self.fetch(
            "/api/v1/notification/550e8400-e29b-41d4-a716-000000000000",
            method="DELETE", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)
