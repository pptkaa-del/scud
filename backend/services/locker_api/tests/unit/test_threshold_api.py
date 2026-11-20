"""Unit-тесты для API пороговых значений датчиков"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.sensor_states import ENUM_RANGE_THRESHOLD_ERROR


class TestThresholdApi(AsyncHTTPTestCase):
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
            "contractor_id": None, "contractor_name": None,
            "created_at": datetime.now(), "created_by": None,
            "updated_at": datetime.now(), "updated_by": None,
            "state": "active", "role": "administrator"
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
            "sensor_type_id": 1,
            "code": "temperature",
            "name": "Температура",
            "unit": "°C",
            "value_type": "numeric",
            "current_value": 25.5,
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
            "is_active": True,
            "metadata": {}
        }
        self.test_threshold = {
            "id": 1,
            "sensor_id": "660e8400-e29b-41d4-a716-446655440001",
            "min_value": -10.0,
            "max_value": 50.0,
            "fixed_value": None,
            "created_at": datetime.now(),
            "created_by": 1,
            "updated_at": datetime.now(),
            "updated_by": 1,
            "is_active": True,
            "description": "Допустимый диапазон температуры"
        }

    def _type_row(self, value_type="numeric", states=None, code="temperature"):
        return {
            "id": 1, "code": code, "name": code, "description": None, "unit": None,
            "value_type": value_type, "created_at": datetime.now(), "states": states,
        }

    def _power_type_row(self):
        return self._type_row("numeric", [
            {"value": 0, "label": "Выкл", "severity": "alert"},
            {"value": 1, "label": "Вкл", "severity": "normal"},
        ], "power")

    def _bool_type_row(self):
        return self._type_row("boolean", [
            {"value": True, "label": "Да", "severity": "alert"},
            {"value": False, "label": "Нет", "severity": "normal"},
        ], "door")

    def test_get_thresholds_list_success(self):
        """Тест: получение списка пороговых значений датчика (paginated envelope)."""
        # sensor_check, count_by_sensor
        self.db_mock.fetch_one.side_effect = [self.test_sensor, {"count": 1}]
        self.db_mock.fetch_all.return_value = [self.test_threshold]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            method="GET",
            headers={"X-Remote-User": "1"}
        )
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["limit"], 100)
        self.assertEqual(data["offset"], 0)
        items = data["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["min_value"], -10.0)
        self.assertEqual(items[0]["max_value"], 50.0)

    def test_get_thresholds_sensor_not_found(self):
        """Тест: датчик не найден при запросе пороговых значений"""
        self.db_mock.fetch_one.return_value = None  # sensor not found

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            method="GET",
            headers={"X-Remote-User": "1"}
        )
        self.assertEqual(response.code, 404)

    def test_get_thresholds_sensor_wrong_locker(self):
        """Тест: датчик не принадлежит указанному шкафу"""
        wrong_sensor = self.test_sensor.copy()
        wrong_sensor["locker_id"] = "other-locker-id"
        self.db_mock.fetch_one.return_value = wrong_sensor

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            method="GET",
            headers={"X-Remote-User": "1"}
        )
        self.assertEqual(response.code, 404)

    def test_create_threshold_success(self):
        """Тест: создание порогового значения"""
        new_threshold = {
            "id": 2,
            "sensor_id": "660e8400-e29b-41d4-a716-446655440001",
            "min_value": 0.0,
            "max_value": 30.0,
            "fixed_value": None,
            "description": "Новый диапазон",
            "created_at": datetime.now(),
            "created_by": 1,
            "updated_at": datetime.now(),
            "updated_by": 1,
            "is_active": True
        }

        self.db_mock.fetch_one.side_effect = [self.test_sensor, self._type_row()]
        self.db_mock.fetch_one_strict.return_value = new_threshold  # INSERT RETURNING *

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        body = json.dumps({
            "min_value": 0.0,
            "max_value": 30.0,
            "description": "Новый диапазон"
        })

        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            method="POST",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=body
        )
        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["min_value"], 0.0)
        self.assertEqual(data["max_value"], 30.0)

    def _boolean_sensor(self):
        sensor = self.test_sensor.copy()
        sensor["code"] = "door"
        sensor["value_type"] = "boolean"
        sensor["current_value"] = False
        sensor["unit"] = None
        return sensor

    def test_create_boolean_threshold_capitalized_normalized(self):
        """fixed_value='True' для boolean-датчика канонизируется в 'true' до записи."""
        bool_sensor = self._boolean_sensor()
        stored = {
            "id": 5,
            "sensor_id": bool_sensor["id"],
            "min_value": None,
            "max_value": None,
            "fixed_value": "true",
            "description": None,
            "created_at": datetime.now(),
            "created_by": 1,
            "updated_at": datetime.now(),
            "updated_by": 1,
            "is_active": True,
        }
        self.db_mock.fetch_one.side_effect = [bool_sensor, self._bool_type_row()]
        self.db_mock.fetch_one_strict.return_value = stored

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = bool_sensor["id"]
        body = json.dumps({"fixed_value": "True"})

        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            method="POST",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=body,
        )
        self.assertEqual(response.code, 201)
        # Репозиторий должен получить уже канонизированное "true", а не "True".
        insert_args = self.db_mock.fetch_one_strict.call_args.args[1]
        self.assertIn("true", insert_args)
        self.assertNotIn("True", insert_args)

    def test_create_boolean_threshold_invalid_rejected(self):
        """fixed_value='banana' для boolean-датчика → 400."""
        bool_sensor = self._boolean_sensor()
        self.db_mock.fetch_one.side_effect = [bool_sensor, self._bool_type_row()]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = bool_sensor["id"]
        body = json.dumps({"fixed_value": "banana"})

        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            method="POST",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=body,
        )
        self.assertEqual(response.code, 400)
        self.db_mock.fetch_one_strict.assert_not_called()

    def test_create_boolean_threshold_valid_false_unchanged(self):
        """fixed_value='false' для boolean-датчика проходит без изменений."""
        bool_sensor = self._boolean_sensor()
        stored = {
            "id": 6,
            "sensor_id": bool_sensor["id"],
            "min_value": None,
            "max_value": None,
            "fixed_value": "false",
            "description": None,
            "created_at": datetime.now(),
            "created_by": 1,
            "updated_at": datetime.now(),
            "updated_by": 1,
            "is_active": True,
        }
        self.db_mock.fetch_one.side_effect = [bool_sensor, self._bool_type_row()]
        self.db_mock.fetch_one_strict.return_value = stored

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = bool_sensor["id"]
        body = json.dumps({"fixed_value": "false"})

        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            method="POST",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=body,
        )
        self.assertEqual(response.code, 201)
        insert_args = self.db_mock.fetch_one_strict.call_args.args[1]
        self.assertIn("false", insert_args)

    def test_create_threshold_missing_values(self):
        """Тест: создание порогового значения без обязательных полей"""
        self.db_mock.fetch_one.return_value = self.test_sensor  # sensor exists

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        body = json.dumps({
            "description": "Без значений"
        })

        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            method="POST",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=body
        )
        self.assertEqual(response.code, 400)

    def test_create_threshold_sensor_not_found(self):
        """Тест: создание порогового значения для несуществующего датчика"""
        self.db_mock.fetch_one.return_value = None  # sensor not found

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        body = json.dumps({
            "min_value": 0.0,
            "max_value": 30.0
        })

        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            method="POST",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=body
        )
        self.assertEqual(response.code, 404)

    def test_get_threshold_success(self):
        """Тест: получение информации о пороговом значении"""
        self.db_mock.fetch_one.side_effect = [
            self.test_threshold,  # threshold exists
            self.test_sensor  # sensor exists and belongs to locker
        ]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        threshold_id = "1"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds/{threshold_id}",
            method="GET",
            headers={"X-Remote-User": "1"}
        )
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["min_value"], -10.0)

    def test_get_threshold_not_found(self):
        """Тест: пороговое значение не найдено"""
        self.db_mock.fetch_one.return_value = None  # threshold not found

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        threshold_id = "999"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds/{threshold_id}",
            method="GET",
            headers={"X-Remote-User": "1"}
        )
        self.assertEqual(response.code, 404)

    def test_update_threshold_success(self):
        """Тест: обновление порогового значения"""
        updated_threshold = self.test_threshold.copy()
        updated_threshold["max_value"] = 60.0
        updated_threshold["description"] = "Расширенный диапазон"

        self.db_mock.fetch_one.side_effect = [
            self.test_threshold,  # threshold exists
            self.test_sensor,  # sensor (max_value задан → проверка перечислимого датчика)
            self._type_row(),
            updated_threshold  # updated threshold
        ]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        threshold_id = "1"
        body = json.dumps({
            "max_value": 60.0,
            "description": "Расширенный диапазон"
        })

        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds/{threshold_id}",
            method="PUT",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=body
        )
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["max_value"], 60.0)
        self.assertEqual(data["description"], "Расширенный диапазон")

    def test_update_threshold_not_found(self):
        """Тест: обновление несуществующего порогового значения"""
        self.db_mock.fetch_one.return_value = None  # threshold not found

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        threshold_id = "999"
        body = json.dumps({
            "max_value": 60.0
        })

        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds/{threshold_id}",
            method="PUT",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=body
        )
        self.assertEqual(response.code, 404)

    def test_delete_threshold_success(self):
        """Тест: удаление порогового значения"""
        self.db_mock.fetch_one.return_value = self.test_threshold  # threshold exists
        self.db_mock.execute.return_value = 1  # DELETE affected 1 row

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        threshold_id = "1"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds/{threshold_id}",
            method="DELETE",
            headers={"X-Remote-User": "1"}
        )
        self.assertEqual(response.code, 204)

    def test_delete_threshold_not_found(self):
        """Тест: удаление несуществующего порогового значения"""
        self.db_mock.fetch_one.return_value = None  # threshold not found

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        threshold_id = "999"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds/{threshold_id}",
            method="DELETE",
            headers={"X-Remote-User": "1"}
        )
        self.assertEqual(response.code, 404)

    def _power_sensor(self):
        sensor = self.test_sensor.copy()
        sensor.update({"code": "power", "name": "Питание", "unit": None,
                       "serial_number": "relay-power", "current_value": 1})
        return sensor

    def _stored(self, sensor, fixed_value):
        return {
            "id": 7, "sensor_id": sensor["id"], "min_value": None, "max_value": None,
            "fixed_value": fixed_value, "description": None,
            "created_at": datetime.now(), "created_by": 1,
            "updated_at": datetime.now(), "updated_by": 1, "is_active": True,
        }

    def _post_threshold(self, sensor, payload):
        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        return self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor['id']}/thresholds",
            method="POST",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=json.dumps(payload),
        )

    def test_range_threshold_rejected_when_type_has_states(self):
        for sensor, type_row in ((self._power_sensor(), self._power_type_row()),
                                 (self._boolean_sensor(), self._bool_type_row())):
            for payload in ({"min_value": 0}, {"max_value": 1}, {"min_value": 0, "fixed_value": 1}):
                with self.subTest(code=sensor["code"], payload=payload):
                    self.db_mock.fetch_one.side_effect = [sensor, type_row]
                    self.db_mock.fetch_one_strict.reset_mock()
                    response = self._post_threshold(sensor, payload)
                    self.assertEqual(response.code, 400)
                    self.assertEqual(json.loads(response.body)["detail"], ENUM_RANGE_THRESHOLD_ERROR)
                    self.db_mock.fetch_one_strict.assert_not_called()

    def test_range_threshold_allowed_when_states_null(self):
        sensor = self._power_sensor()
        self.db_mock.fetch_one.side_effect = [sensor, self._type_row(code="power")]
        self.db_mock.fetch_one_strict.return_value = dict(self.test_threshold)
        response = self._post_threshold(sensor, {"min_value": 0, "max_value": 1})
        self.assertEqual(response.code, 201)

    def test_create_numeric_enum_fixed_value_canonicalised(self):
        sensor = self._power_sensor()
        for raw in ("1", 1, 1.0):
            with self.subTest(raw=raw):
                self.db_mock.fetch_one.side_effect = [sensor, self._power_type_row()]
                self.db_mock.fetch_one_strict.reset_mock()
                self.db_mock.fetch_one_strict.return_value = self._stored(sensor, "1")
                response = self._post_threshold(sensor, {"fixed_value": raw})
                self.assertEqual(response.code, 201)
                insert_args = self.db_mock.fetch_one_strict.call_args.args[1]
                self.assertEqual(insert_args[3], "1")

    def test_create_numeric_enum_fixed_value_out_of_vocabulary_rejected(self):
        sensor = self._power_sensor()
        for raw in ("5", "abc", True, 0.5):
            with self.subTest(raw=raw):
                self.db_mock.fetch_one.side_effect = [sensor, self._power_type_row()]
                self.db_mock.fetch_one_strict.reset_mock()
                response = self._post_threshold(sensor, {"fixed_value": raw})
                self.assertEqual(response.code, 400)
                self.assertIn("fixed_value должен быть одним из: 0, 1",
                              json.loads(response.body)["detail"])
                self.db_mock.fetch_one_strict.assert_not_called()

    def test_fixed_value_must_be_in_db_states(self):
        sensor = self._power_sensor()
        type_row = self._type_row("numeric", [
            {"value": 0, "label": "Выкл", "severity": "alert"},
            {"value": 1, "label": "Вкл", "severity": "normal"},
            {"value": 3, "label": "Резерв", "severity": "warning"},
        ], "power")
        self.db_mock.fetch_one.side_effect = [sensor, type_row]
        response = self._post_threshold(sensor, {"fixed_value": 2})
        self.assertEqual(response.code, 400)
        self.assertIn("0, 1, 3", json.loads(response.body)["detail"])
        self.db_mock.fetch_one_strict.assert_not_called()

    def test_create_range_threshold_on_plain_numeric_sensor_still_allowed(self):
        self.db_mock.fetch_one.side_effect = [self.test_sensor, self._type_row()]
        self.db_mock.fetch_one_strict.return_value = dict(self.test_threshold)
        response = self._post_threshold(self.test_sensor, {"min_value": 1, "max_value": 2})
        self.assertEqual(response.code, 201)

    def test_update_range_threshold_on_enum_sensor_rejected(self):
        sensor = self._power_sensor()
        threshold = dict(self.test_threshold, sensor_id=sensor["id"])
        self.db_mock.fetch_one.side_effect = [threshold, sensor, self._power_type_row()]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor['id']}/thresholds/1",
            method="PUT",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=json.dumps({"max_value": 1}),
        )
        self.assertEqual(response.code, 400)
        self.assertEqual(self.db_mock.fetch_one.call_count, 3)

    def test_update_numeric_enum_fixed_value_canonicalised(self):
        sensor = self._power_sensor()
        threshold = dict(self.test_threshold, sensor_id=sensor["id"], min_value=None,
                         max_value=None, fixed_value="0")
        self.db_mock.fetch_one.side_effect = [
            threshold, sensor, self._power_type_row(), dict(threshold, fixed_value="1")]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/sensors/{sensor['id']}/thresholds/1",
            method="PUT",
            headers={"X-Remote-User": "1", "Content-Type": "application/json"},
            body=json.dumps({"fixed_value": 1.0}),
        )
        self.assertEqual(response.code, 200)
        update_args = self.db_mock.fetch_one.call_args_list[3].args[1]
        self.assertEqual(update_args[2], "1")
