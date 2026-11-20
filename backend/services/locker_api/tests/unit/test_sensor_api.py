"""Unit-тесты для API датчиков"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app

LOCKER_ID = "550e8400-e29b-41d4-a716-446655440000"
OPERATOR_HEADERS = {"X-Remote-User": "1"}

STATES_BY_CODE = {
    "power": [
        {"value": 0, "label": "От батареи", "severity": "alert"},
        {"value": 1, "label": "От сети", "severity": "normal"},
    ],
    "error": [
        {"value": False, "label": "Нет ошибки", "severity": "normal"},
        {"value": True, "label": "Ошибка", "severity": "alert"},
    ],
    "door": [
        {"value": False, "label": "Закрыта", "severity": "normal"},
        {"value": True, "label": "Открыта", "severity": "alert"},
    ],
}


class TestSensorApi(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()

        self.command_queue_mock = MagicMock()
        return make_app(self.db_mock, self.command_queue_mock)

    def setUp(self):
        super().setUp()
        # Clear mock return values and side effects before each test
        self.db_mock.fetch_one.return_value = None
        self.db_mock.fetch_one.side_effect = None
        self.db_mock.fetch_all.return_value = []
        self.db_mock.fetch_all.side_effect = None
        self.db_mock.execute.return_value = 0
        self.db_mock.execute.side_effect = None

        self.auth_user_row = {
            "id": 1, "phone": "+79123456789", "full_name": "Admin",
            "contractor_id": None, "created_at": datetime.now(),
            "created_by": None, "updated_at": datetime.now(),
            "updated_by": None, "state": "active"
        }
        self.test_locker = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "inventory_number": "СО №777", "location_address": "ул. Тестовая, 1",
            "location_lat": 55.751244, "location_lon": 37.618423,
            "auth_key": "test_key", "created_at": datetime.now(),
            "created_by": 1, "updated_at": datetime.now(),
            "updated_by": 1, "connectivity": "offline", "door": "closed",
            "operational_status": "normal",
            "last_seen_at": None, "last_opened_at": None, "description": "Тест", "metadata": {}
        }
        self.test_sensor = {
            "id": "660e8400-e29b-41d4-a716-446655440001",
            "locker_id": "550e8400-e29b-41d4-a716-446655440000",
            "serial_number": "TEMP-001",
            "sensor_type_id": 1,  # FK к sensor_types (температура)
            "code": "temperature",  # from sensor_types
            "name": "Температура",  # from sensor_types
            "unit": "°C",  # from sensor_types
            "value_type": "numeric",  # from sensor_types
            "current_value": 25.5,
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
            "is_active": True,
            "metadata": {}
        }

    def test_get_sensors_list_success(self):
        """Тест: получение списка датчиков шкафа (paginated envelope)."""
        # locker_check, count_by_locker
        self.db_mock.fetch_one.side_effect = [self.test_locker, {"count": 1}]
        self.db_mock.fetch_all.return_value = [self.test_sensor]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(f"/api/v1/locker/{locker_id}/sensors",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["limit"], 100)
        self.assertEqual(data["offset"], 0)
        items = data["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["serial_number"], "TEMP-001")
        # Проверяем поля из sensor_types через JOIN
        self.assertEqual(items[0]["code"], "temperature")
        self.assertEqual(items[0]["name"], "Температура")
        self.assertEqual(items[0]["unit"], "°C")
        self.assertEqual(items[0]["value_type"], "numeric")

    def test_get_sensors_locker_not_found(self):
        """Тест: шкаф не найден при запросе датчиков"""
        self.db_mock.fetch_one.return_value = None  # locker not found

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(f"/api/v1/locker/{locker_id}/sensors",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)

    def test_create_sensor_success(self):
        """Тест: создание датчика"""
        new_sensor = self.test_sensor.copy()
        new_sensor["serial_number"] = "TEMP-002"

        # Мок для sensor_type
        test_sensor_type = {
            "id": 1,
            "code": "temperature",
            "name": "Температура",
            "description": "Датчик температуры",
            "unit": "°C",
            "value_type": "numeric",
            "created_at": datetime.now()
        }

        self.db_mock.fetch_one.side_effect = [
            self.test_locker,   # locker exists
            test_sensor_type,   # sensor_type exists
            new_sensor,         # get_by_id (после INSERT)
        ]
        self.db_mock.fetch_one_strict.return_value = {
            "id": "660e8400-e29b-41d4-a716-446655440002",  # INSERT RETURNING id
        }

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        body = json.dumps({
            "serial_number": "TEMP-002",
            "sensor_type_id": 1,  # Используем ID типа датчика вместо строки "type"
            "current_value": 22.0
        })

        response = self.fetch(f"/api/v1/locker/{locker_id}/sensors",
                              method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["serial_number"], "TEMP-002")
        # Проверяем поля из sensor_types
        self.assertEqual(data["code"], "temperature")
        self.assertEqual(data["name"], "Температура")
        self.assertEqual(data["unit"], "°C")
        self.assertEqual(data["value_type"], "numeric")

    def test_create_sensor_invalid_type(self):
        """Тест: создание датчика с несуществующим sensor_type_id"""
        self.db_mock.fetch_one.side_effect = [
            self.test_locker,  # locker exists
            None  # sensor_type не найден
        ]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        body = json.dumps({
            "serial_number": "TEMP-002",
            "sensor_type_id": 9999  # Несуществующий ID типа датчика
        })

        response = self.fetch(f"/api/v1/locker/{locker_id}/sensors",
                              method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 404)  # Handler возвращает 404 для несуществующего sensor_type

    def test_update_sensor_success(self):
        """Тест: успешное обновление статуса активности датчика"""
        updated_sensor = self.test_sensor.copy()
        updated_sensor["is_active"] = False

        self.db_mock.fetch_one.side_effect = [
            self.test_sensor,  # get_by_id для проверки существования
            updated_sensor  # update_active_status возвращает обновленный датчик
        ]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        body = json.dumps({
            "is_active": False
        })

        response = self.fetch(f"/api/v1/locker/{locker_id}/sensors/{sensor_id}",
                              method="PATCH",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["is_active"], False)

    def test_delete_sensor_success(self):
        """Тест: успешное удаление датчика"""
        self.db_mock.fetch_one.return_value = self.test_sensor  # get_by_id возвращает полный объект Sensor
        self.db_mock.execute.return_value = 1  # DELETE успешно удалил 1 строку

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        response = self.fetch(f"/api/v1/locker/{locker_id}/sensors/{sensor_id}",
                              method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 204)

    def test_get_sensor_detail_not_found(self):
        """Тест: получение несуществующего датчика"""
        self.db_mock.fetch_one.return_value = None

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440999"
        response = self.fetch(f"/api/v1/locker/{locker_id}/sensors/{sensor_id}",
                              method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)

    def _relay_sensor(self, code, value_type, current_value, sensor_id):
        sensor = self.test_sensor.copy()
        sensor.update({
            "id": sensor_id, "serial_number": f"relay-{code}", "code": code,
            "name": code, "unit": None, "value_type": value_type,
            "current_value": {"v": current_value} if current_value is not None else None,
            "states": STATES_BY_CODE.get(code),
        })
        return sensor

    def test_get_sensors_list_includes_display(self):
        """display вычисляется по коду: подпись для перечислимых, null для остальных."""
        rows = [
            self._relay_sensor("power", "numeric", 1, "00000000-0000-0000-0000-000000000001"),
            self._relay_sensor("power", "numeric", None, "00000000-0000-0000-0000-000000000002"),
            self._relay_sensor("error", "boolean", True, "00000000-0000-0000-0000-000000000003"),
            self.test_sensor,
        ]
        self.db_mock.fetch_one.side_effect = [self.test_locker, {"count": len(rows)}]
        self.db_mock.fetch_all.return_value = rows

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(f"/api/v1/locker/{locker_id}/sensors",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        items = {item["id"]: item for item in json.loads(response.body)["items"]}
        mains = items["00000000-0000-0000-0000-000000000001"]
        self.assertEqual(mains["display"], {"label": "От сети", "severity": "normal"})
        self.assertEqual(mains["current_value"], 1)
        self.assertEqual(items["00000000-0000-0000-0000-000000000002"]["display"],
                         {"label": "Нет данных", "severity": "normal"})
        self.assertEqual(items["00000000-0000-0000-0000-000000000003"]["display"],
                         {"label": "Ошибка", "severity": "alert"})
        self.assertIn("display", items[self.test_sensor["id"]])
        self.assertIsNone(items[self.test_sensor["id"]]["display"])

    def test_get_sensor_detail_includes_display(self):
        door = self._relay_sensor("door", "boolean", False, self.test_sensor["id"])
        self.db_mock.fetch_one.return_value = door

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(f"/api/v1/locker/{locker_id}/sensors/{door['id']}",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)["display"],
                         {"label": "Закрыта", "severity": "normal"})

    def _list_display(self, row):
        self.db_mock.fetch_all.return_value = [row]
        self.db_mock.fetch_one.side_effect = [self.test_locker, {"count": 1}]
        response = self.fetch(f"/api/v1/locker/{LOCKER_ID}/sensors", headers=OPERATOR_HEADERS)
        return response, json.loads(response.body)

    def test_display_comes_from_row_states(self):
        row = {**self.test_sensor, "code": "power", "value_type": "numeric",
               "current_value": {"v": 0},
               "states": [{"value": 0, "label": "Батарея", "severity": "alert"}]}
        _, data = self._list_display(row)
        assert data["items"][0]["display"] == {"label": "Батарея", "severity": "alert"}

    def test_no_states_gives_null_display(self):
        row = {**self.test_sensor, "code": "power", "current_value": {"v": 0}, "states": None}
        _, data = self._list_display(row)
        assert data["items"][0]["display"] is None

    def test_corrupt_states_in_row_gives_null_display(self):
        row = {**self.test_sensor, "code": "power", "current_value": {"v": 0},
               "states": [{"value": "bad"}]}
        response, data = self._list_display(row)
        assert response.code == 200
        assert data["items"][0]["display"] is None
