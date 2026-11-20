"""Unit-тесты для API подрядчиков"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.user_data.db.models.user import UserState


class TestContractorApi(AsyncHTTPTestCase):
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
            "id": 1, "phone": "+79001112233", "full_name": "Admin", "contractor_id": None,
            "contractor_name": None,
            "created_at": datetime.now(), "created_by": None, "updated_at": datetime.now(), "updated_by": None,
            "state": UserState.ACTIVE.value, "role": "administrator"
        }

    def test_get_contractors_success(self):
        """Тест: успешное получение списка подрядчиков (paginated envelope)."""
        # auth_user, count(), get_all (через fetch_all)
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 2}]
        self.db_mock.fetch_all.return_value = [
            {"id": 1, "name": "Contractor 1", "created_at": datetime.now(), "created_by": 1},
            {"id": 2, "name": "Contractor 2", "created_at": datetime.now(), "created_by": 1}
        ]

        response = self.fetch("/api/v1/contractors", method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["limit"], 100)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][0]["name"], "Contractor 1")

    def test_get_contractors_with_search_filter(self):
        """`?q=` применяет ILIKE-фильтр (с ESCAPE) и учитывает его в total."""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 1}]
        self.db_mock.fetch_all.return_value = [
            {"id": 1, "name": "ООО Стройка", "created_at": datetime.now(), "created_by": 1},
        ]

        response = self.fetch(
            "/api/v1/contractors?q=Стр",
            method="GET",
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["name"], "ООО Стройка")

        # Проверяем, что ILIKE с ESCAPE действительно ушёл в SQL (и в get_all, и в count).
        get_all_args = self.db_mock.fetch_all.call_args
        self.assertIn("name ILIKE %s ESCAPE '\\'", get_all_args[0][0])
        self.assertIn("%Стр%", get_all_args[0][1])

        count_args = self.db_mock.fetch_one.call_args  # последний fetch_one — это count()
        self.assertIn("name ILIKE %s ESCAPE '\\'", count_args[0][0])
        self.assertIn("%Стр%", count_args[0][1])

    def test_get_contractors_empty_search_treated_as_no_filter(self):
        """`?q=` (пустая строка) трактуется как «фильтр не задан» — без ILIKE в SQL."""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 0}]
        self.db_mock.fetch_all.return_value = []

        response = self.fetch(
            "/api/v1/contractors?q=",
            method="GET",
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(response.code, 200)

        get_all_sql = self.db_mock.fetch_all.call_args[0][0]
        self.assertNotIn("ILIKE", get_all_sql)

        count_sql = self.db_mock.fetch_one.call_args[0][0]
        self.assertNotIn("ILIKE", count_sql)

    def test_get_contractors_whitespace_search_treated_as_no_filter(self):
        """`?q=` (только пробелы) после strip трактуется как «фильтр не задан»."""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 0}]
        self.db_mock.fetch_all.return_value = []

        response = self.fetch(
            "/api/v1/contractors?q=%20%20",
            method="GET",
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(response.code, 200)

        get_all_sql = self.db_mock.fetch_all.call_args[0][0]
        self.assertNotIn("ILIKE", get_all_sql)

    def test_get_contractors_search_escapes_like_metachars(self):
        """`?q=50%` экранирует LIKE-метасимвол: в параметрах — `%50\\%%`."""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 0}]
        self.db_mock.fetch_all.return_value = []

        response = self.fetch(
            "/api/v1/contractors?q=50%25",  # %25 == '%'
            method="GET",
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(response.code, 200)

        get_all_args = self.db_mock.fetch_all.call_args
        self.assertIn("%50\\%%", get_all_args[0][1])

    def test_get_contractors_search_too_long_returns_400(self):
        """`?q=` длиннее 255 символов → 400 (валидационная ошибка)."""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row]

        long_q = "a" * 256
        response = self.fetch(
            f"/api/v1/contractors?q={long_q}",
            method="GET",
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(response.code, 400)

    def test_create_contractor_success(self):
        """Тест: успешное создание подрядчика"""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row]
        self.db_mock.fetch_one_strict.return_value = {
            "id": 10, "name": "New Contractor",
            "created_at": datetime.now(), "created_by": 1,
        }

        body = {"name": "New Contractor"}
        response = self.fetch("/api/v1/contractors", method="POST",
                            body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["name"], "New Contractor")

    def test_create_contractor_missing_name(self):
        """Тест: создание подрядчика без указания имени"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {}
        response = self.fetch("/api/v1/contractors", method="POST",
                            body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertEqual(data["status"], 400)
        self.assertIn("name", data["detail"])

    def test_get_contractor_by_id_success(self):
        """Тест: успешное получение подрядчика по ID"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            {"id": 5, "name": "Test Contractor", "created_at": datetime.now(), "created_by": 1}
        ]

        response = self.fetch("/api/v1/contractor/5", method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["id"], 5)
        self.assertEqual(data["name"], "Test Contractor")

    def test_get_contractor_not_found(self):
        """Тест: подрядчик не найден"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            None
        ]

        response = self.fetch("/api/v1/contractor/999", method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Not Found")
        self.assertEqual(data["status"], 404)
        self.assertIn("Contractor", data["detail"])
        self.assertIn("not found", data["detail"])

    def test_update_contractor_success(self):
        """Тест: успешное обновление подрядчика"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            {"id": 5, "name": "Updated Contractor", "created_at": datetime.now(), "created_by": 1}
        ]

        body = {"name": "Updated Contractor"}
        response = self.fetch("/api/v1/contractor/5", method="PUT",
                            body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["name"], "Updated Contractor")

    def test_update_contractor_not_found(self):
        """Тест: обновление несуществующего подрядчика"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            None
        ]

        body = {"name": "Updated Name"}
        response = self.fetch("/api/v1/contractor/999", method="PUT",
                            body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Not Found")
        self.assertEqual(data["status"], 404)
        self.assertIn("Contractor", data["detail"])
        self.assertIn("not found", data["detail"])

    def test_delete_contractor_success(self):
        """Тест: успешное soft-delete подрядчика (UPDATE state='archived')"""
        # 1) auth-check, 2) get_by_id (existence), 3) UPDATE...RETURNING id
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            {"id": 5, "name": "To Delete", "created_at": datetime.now(), "created_by": 1},
            {"id": 5},
        ]

        response = self.fetch("/api/v1/contractor/5", method="DELETE", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 204)
        # Soft-delete делает UPDATE через fetch_one (для RETURNING id), не execute
        self.assertEqual(self.db_mock.fetch_one.call_count, 3)

    def test_delete_contractor_not_found(self):
        """Тест: удаление несуществующего подрядчика"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            None
        ]

        response = self.fetch("/api/v1/contractor/999", method="DELETE", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Not Found")
        self.assertEqual(data["status"], 404)
        self.assertIn("Contractor", data["detail"])
        self.assertIn("not found", data["detail"])
