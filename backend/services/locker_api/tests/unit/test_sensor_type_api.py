"""Unit-тесты для API типов датчиков"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

import psycopg2.errors
from tornado.testing import AsyncHTTPTestCase

from app.handlers.sensor_type import STATES_FIXED_VALUE_CONFLICT, STATES_RANGE_CONFLICT
from app.main import make_app


class TestSensorTypeApi(AsyncHTTPTestCase):
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
        self.test_sensor_type = {
            "id": 1,
            "code": "temperature",
            "name": "Температура",
            "description": "Датчик температуры",
            "unit": "°C",
            "value_type": "numeric",
            "created_at": datetime.now()
        }

    def test_get_sensor_types_list_success(self):
        """Тест: получение списка типов датчиков (paginated envelope)."""
        self.db_mock.fetch_all.return_value = [self.test_sensor_type]
        self.db_mock.fetch_one.return_value = {"count": 1}

        response = self.fetch("/api/v1/sensor-types",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["limit"], 100)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["code"], "temperature")
        self.assertEqual(data["items"][0]["name"], "Температура")

    def test_get_sensor_types_empty_list(self):
        """Тест: получение пустого списка типов датчиков."""
        self.db_mock.fetch_all.return_value = []
        self.db_mock.fetch_one.return_value = {"count": 0}

        response = self.fetch("/api/v1/sensor-types",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["items"], [])

    def test_create_sensor_type_success(self):
        """Тест: создание нового типа датчика"""
        new_sensor_type = {
            "id": 7,
            "code": "pressure",
            "name": "Давление",
            "description": "Датчик давления",
            "unit": "kPa",
            "value_type": "numeric",
            "created_at": datetime.now()
        }

        self.db_mock.fetch_one.side_effect = [None]  # get_by_code - not exists
        self.db_mock.fetch_one_strict.return_value = new_sensor_type  # INSERT RETURNING *

        body = json.dumps({
            "code": "pressure",
            "name": "Давление",
            "description": "Датчик давления",
            "unit": "kPa",
            "value_type": "numeric"
        })

        response = self.fetch("/api/v1/sensor-types",
                              method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["code"], "pressure")
        self.assertEqual(data["name"], "Давление")

    def test_create_sensor_type_missing_code(self):
        """Тест: создание типа датчика без кода"""
        body = json.dumps({
            "name": "Давление",
            "value_type": "numeric"
        })

        response = self.fetch("/api/v1/sensor-types",
                              method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 400)

    def test_create_sensor_type_missing_name(self):
        """Тест: создание типа датчика без названия"""
        body = json.dumps({
            "code": "pressure",
            "value_type": "numeric"
        })

        response = self.fetch("/api/v1/sensor-types",
                              method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 400)

    def test_create_sensor_type_invalid_value_type(self):
        """Тест: создание типа датчика с некорректным value_type"""
        body = json.dumps({
            "code": "pressure",
            "name": "Давление",
            "value_type": "invalid_type"
        })

        response = self.fetch("/api/v1/sensor-types",
                              method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 400)

    def test_create_sensor_type_duplicate_code(self):
        """Тест: создание типа датчика с существующим кодом"""
        self.db_mock.fetch_one.return_value = self.test_sensor_type  # code already exists

        body = json.dumps({
            "code": "temperature",
            "name": "Температура 2",
            "value_type": "numeric"
        })

        response = self.fetch("/api/v1/sensor-types",
                              method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 409)

    def test_get_sensor_type_success(self):
        """Тест: получение информации о типе датчика"""
        self.db_mock.fetch_one.return_value = self.test_sensor_type

        response = self.fetch("/api/v1/sensor-types/1",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["code"], "temperature")

    def test_get_sensor_type_not_found(self):
        """Тест: тип датчика не найден"""
        self.db_mock.fetch_one.return_value = None

        response = self.fetch("/api/v1/sensor-types/999",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)

    def test_update_sensor_type_success(self):
        """Тест: обновление типа датчика"""
        updated_sensor_type = self.test_sensor_type.copy()
        updated_sensor_type["name"] = "Температура окружающей среды"
        updated_sensor_type["description"] = "Обновленное описание"

        self.db_mock.fetch_one.return_value = updated_sensor_type

        body = json.dumps({
            "name": "Температура окружающей среды",
            "description": "Обновленное описание"
        })

        response = self.fetch("/api/v1/sensor-types/1",
                              method="PUT",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["name"], "Температура окружающей среды")
        self.assertEqual(data["description"], "Обновленное описание")

    def test_update_sensor_type_not_found(self):
        """Тест: обновление несуществующего типа датчика"""
        self.db_mock.fetch_one.return_value = None

        body = json.dumps({
            "name": "Новое название"
        })

        response = self.fetch("/api/v1/sensor-types/999",
                              method="PUT",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 404)

    def test_update_sensor_type_invalid_value_type(self):
        """Тест: PUT с некорректным value_type → 400"""
        body = json.dumps({
            "value_type": "invalid_type"
        })

        response = self.fetch("/api/v1/sensor-types/1",
                              method="PUT",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 400)

    def test_create_sensor_type_all_valid_value_types(self):
        """Тест: каждое значение SensorValueType (numeric/boolean/string) принимается."""
        for value_type in ("numeric", "boolean", "string"):
            with self.subTest(value_type=value_type):
                created = {
                    "id": 99, "code": f"sensor_{value_type}", "name": "test",
                    "description": None, "unit": None,
                    "value_type": value_type, "created_at": datetime.now(),
                }
                self.db_mock.fetch_one.side_effect = [None]
                self.db_mock.fetch_one_strict.return_value = created

                body = json.dumps({
                    "code": f"sensor_{value_type}",
                    "name": "test",
                    "value_type": value_type,
                })
                response = self.fetch("/api/v1/sensor-types",
                                      method="POST",
                                      headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                                      body=body)
                self.assertEqual(response.code, 201, f"value_type={value_type} should be accepted")

    def test_delete_sensor_type_success(self):
        """Тест: удаление типа датчика"""
        self.db_mock.fetch_one.return_value = self.test_sensor_type
        self.db_mock.execute.return_value = 1  # DELETE affected 1 row

        response = self.fetch("/api/v1/sensor-types/1",
                              method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 204)

    def test_delete_sensor_type_not_found(self):
        """Тест: удаление несуществующего типа датчика"""
        self.db_mock.fetch_one.return_value = None

        response = self.fetch("/api/v1/sensor-types/999",
                              method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)

    def test_delete_sensor_type_with_sensors(self):
        """COR-26: удаление типа датчика, на который ссылаются датчики → 409.

        На реальной БД DELETE родителя с FK-зависимостью поднимает
        ForeignKeyViolation (23503), а НЕ возвращает None. Раньше int()-обёртки
        не было: raw psycopg2-ошибка долетала до write_error → 500, а 409-ветка
        была недостижима. Теперь handler ловит FK-violation и отдаёт 409.
        """
        self.db_mock.fetch_one.side_effect = [
            self.test_sensor_type,  # get_by_id - тип существует
            psycopg2.errors.ForeignKeyViolation("update or delete on table "
                                                "\"sensor_types\" violates FK"),
        ]

        response = self.fetch("/api/v1/sensor-types/1",
                              method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 409)

    def test_delete_sensor_type_vanished_after_check(self):
        """DELETE RETURNING id → None (запись исчезла после get_by_id) → 404."""
        self.db_mock.fetch_one.side_effect = [
            self.test_sensor_type,  # get_by_id - тип существует
            None,  # DELETE RETURNING id ничего не вернул (гонка с другим DELETE)
        ]

        response = self.fetch("/api/v1/sensor-types/1",
                              method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)

    def _power_type(self, **overrides):
        row = {
            "id": 4, "code": "power", "name": "Питание",
            "description": "Источник питания релейной платы: 0 — от батареи, 1 — от сети",
            "unit": None, "value_type": "numeric", "created_at": datetime.now(),
            "states": [{"value": 0, "label": "От батареи", "severity": "warning"},
                       {"value": 1, "label": "От сети", "severity": "normal"}],
        }
        row.update(overrides)
        return row

    def _put(self, type_id, payload):
        return self.fetch(f"/api/v1/sensor-types/{type_id}", method="PUT",
                          headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                          body=json.dumps(payload))

    def test_list_exposes_states(self):
        self.db_mock.fetch_all.return_value = [self._power_type(), self.test_sensor_type]
        self.db_mock.fetch_one.return_value = {"count": 2}

        response = self.fetch("/api/v1/sensor-types", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        items = {item["code"]: item for item in json.loads(response.body)["items"]}
        self.assertEqual(items["power"]["states"], [
            {"value": 0, "label": "От батареи", "severity": "warning"},
            {"value": 1, "label": "От сети", "severity": "normal"},
        ])
        self.assertIsNone(items["temperature"]["states"])

    def test_get_boolean_type_states_are_bools(self):
        self.db_mock.fetch_one.return_value = self._power_type(
            id=11, code="error", name="Ошибка платы",
            description="Плата сообщила об ошибке (stat = 2)", value_type="boolean",
            states=[{"value": False, "label": "Норма", "severity": "normal"},
                    {"value": True, "label": "Ошибка", "severity": "alert"}])
        response = self.fetch("/api/v1/sensor-types/11", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        states = json.loads(response.body)["states"]
        self.assertEqual([s["value"] for s in states], [False, True])
        self.assertEqual(states[1], {"value": True, "label": "Ошибка", "severity": "alert"})

    def _post(self, payload):
        return self.fetch("/api/v1/sensor-types", method="POST",
                          headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                          body=json.dumps(payload))

    def _update_calls(self):
        return [c for c in self.db_mock.fetch_one.call_args_list
                if "UPDATE sensor_types" in c.args[0]]

    def test_post_with_states(self):
        created = {**self.test_sensor_type, "code": "mode",
                   "states": [{"value": 0, "label": "Выкл", "severity": "normal"}]}
        self.db_mock.fetch_one.side_effect = [None]
        self.db_mock.fetch_one_strict.return_value = created
        response = self._post({"code": "mode", "name": "Режим", "value_type": "numeric",
                               "states": [{"value": 0, "label": "Выкл", "severity": "normal"}]})
        self.assertEqual(response.code, 201)
        sql, params = self.db_mock.fetch_one_strict.await_args.args[:2]
        self.assertIn("states", sql)
        self.assertEqual(json.loads(params[-1]),
                         [{"value": 0, "label": "Выкл", "severity": "normal"}])
        self.assertEqual(json.loads(response.body)["states"][0]["label"], "Выкл")

    def test_post_invalid_states_400(self):
        for states in ([{"value": True, "label": "x", "severity": "normal"}],
                       [], [{"value": 0, "label": "x", "severity": "fatal"}]):
            with self.subTest(states=states):
                response = self._post({"code": "mode", "name": "Режим",
                                       "value_type": "numeric", "states": states})
                self.assertEqual(response.code, 400)
        self.db_mock.fetch_one_strict.assert_not_awaited()

    def test_put_without_states_key_keeps_states(self):
        updated = self._power_type(description="Новое описание")
        self.db_mock.fetch_one.side_effect = [self._power_type(), self._power_type(), updated]
        response = self._put(4, {"description": "Новое описание"})
        self.assertEqual(response.code, 200)
        sql, params = self._update_calls()[-1].args[:2]
        self.assertNotIn("states", sql.split("RETURNING")[0])
        self.assertEqual(params[-1], 4)
        self.assertEqual(len(json.loads(response.body)["states"]), 2)
        self.db_mock.fetch_all.assert_not_awaited()

    def test_put_states_null_clears(self):
        cleared = self._power_type(states=None)
        self.db_mock.fetch_one.side_effect = [self._power_type(), self._power_type(), cleared]
        response = self._put(4, {"states": None})
        self.assertEqual(response.code, 200)
        sql, params = self._update_calls()[-1].args[:2]
        self.assertIn("states = %s::jsonb", sql)
        self.assertIsNone(params[-2])
        self.assertIsNone(json.loads(response.body)["states"])
        self.db_mock.fetch_all.assert_not_awaited()

    def test_put_states_replaces_list(self):
        new_states = [{"value": 0, "label": "Батарея", "severity": "alert"},
                      {"value": 1, "label": "Сеть", "severity": "normal"}]
        updated = self._power_type(states=new_states)
        self.db_mock.fetch_one.side_effect = [self._power_type(), self._power_type(), updated]
        self.db_mock.fetch_all.return_value = [{"fixed_value": "0"}]
        response = self._put(4, {"states": new_states})
        self.assertEqual(response.code, 200)
        sql, params = self._update_calls()[-1].args[:2]
        self.assertIn("states = %s::jsonb", sql)
        self.assertEqual(json.loads(params[-2]), new_states)
        self.assertEqual(json.loads(response.body)["states"], new_states)

    def test_put_invalid_states_400(self):
        self.db_mock.fetch_one.side_effect = [self._power_type()]
        response = self._put(4, {"states": [{"value": 0, "label": " ", "severity": "normal"}]})
        self.assertEqual(response.code, 400)
        self.assertEqual(self._update_calls(), [])

    def test_put_value_type_change_revalidates_existing_states(self):
        current = {**self.test_sensor_type, "code": "power", "value_type": "numeric",
                   "states": [{"value": 0, "label": "От батареи", "severity": "warning"}]}
        self.db_mock.fetch_one.side_effect = [current]
        response = self._put(1, {"value_type": "boolean"})
        self.assertEqual(response.code, 400)
        self.assertEqual(self._update_calls(), [])

    def test_put_value_type_change_validates_new_states(self):
        bool_states = [{"value": False, "label": "Нет", "severity": "normal"},
                       {"value": True, "label": "Да", "severity": "alert"}]
        current = self._power_type()
        updated = self._power_type(value_type="boolean", states=bool_states)
        self.db_mock.fetch_one.side_effect = [current, current, updated]
        response = self._put(4, {"value_type": "boolean", "states": bool_states})
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)["states"], bool_states)

    def test_put_states_with_range_threshold_conflicts(self):
        self.db_mock.fetch_one.side_effect = [self.test_sensor_type, {"count": 1}]
        response = self._put(1, {"states": [{"value": 0, "label": "Холодно",
                                             "severity": "normal"}]})
        self.assertEqual(response.code, 409)
        self.assertEqual(json.loads(response.body)["detail"], STATES_RANGE_CONFLICT)
        range_sql, range_params = self.db_mock.fetch_one.call_args_list[1].args[:2]
        self.assertIn("sensor_thresholds", range_sql)
        self.assertIn("min_value IS NOT NULL", range_sql)
        self.assertIn("s.is_active = true", range_sql)
        self.assertEqual(range_params, (1,))
        self.assertEqual(self._update_calls(), [])

    def test_put_relabel_enum_type_skips_range_check(self):
        new_states = [{"value": 0, "label": "Батарея", "severity": "alert"},
                      {"value": 1, "label": "Сеть", "severity": "normal"}]
        updated = self._power_type(states=new_states)
        self.db_mock.fetch_one.side_effect = [self._power_type(), self._power_type(), updated]
        response = self._put(4, {"states": new_states})
        self.assertEqual(response.code, 200)
        range_calls = [c for c in self.db_mock.fetch_one.call_args_list
                       if "min_value IS NOT NULL" in c.args[0]]
        self.assertEqual(range_calls, [])
        self.db_mock.fetch_all.assert_awaited_once()

    def test_put_states_dropping_used_fixed_value_conflicts(self):
        current = self._power_type(id=5, code="net_type", states=[
            {"value": 0, "label": "Не определён", "severity": "warning"},
            {"value": 1, "label": "Ethernet", "severity": "normal"},
            {"value": 2, "label": "GSM", "severity": "normal"}])
        self.db_mock.fetch_one.side_effect = [current]
        self.db_mock.fetch_all.return_value = [{"fixed_value": "2"}, {"fixed_value": "1"}]
        response = self._put(5, {"states": [
            {"value": 0, "label": "Не определён", "severity": "warning"},
            {"value": 1, "label": "Ethernet", "severity": "normal"}]})
        self.assertEqual(response.code, 409)
        self.assertEqual(json.loads(response.body)["detail"],
                         STATES_FIXED_VALUE_CONFLICT.format(values="2"))
        fixed_sql, fixed_params = self.db_mock.fetch_all.await_args.args[:2]
        self.assertIn("fixed_value IS NOT NULL", fixed_sql)
        self.assertIn("s.is_active = true", fixed_sql)
        self.assertEqual(fixed_params, (5,))
        self.assertEqual(self._update_calls(), [])

    def test_put_boolean_states_keep_used_fixed_value(self):
        bool_states = [{"value": False, "label": "Норма", "severity": "normal"},
                       {"value": True, "label": "Ошибка", "severity": "alert"}]
        current = self._power_type(id=11, code="error", value_type="boolean", states=bool_states)
        self.db_mock.fetch_one.side_effect = [current, current, current]
        self.db_mock.fetch_all.return_value = [{"fixed_value": "TRUE "}]
        response = self._put(11, {"states": bool_states})
        self.assertEqual(response.code, 200)

    def test_put_boolean_states_legacy_numeric_fixed_value_ok(self):
        bool_states = [{"value": False, "label": "Норма", "severity": "normal"},
                       {"value": True, "label": "Ошибка", "severity": "alert"}]
        current = self._power_type(id=11, code="error", value_type="boolean", states=bool_states)
        self.db_mock.fetch_one.side_effect = [current, current, current]
        self.db_mock.fetch_all.return_value = [{"fixed_value": "1"}, {"fixed_value": "0"}]
        response = self._put(11, {"states": bool_states})
        self.assertEqual(response.code, 200)

    def test_put_numeric_states_float_fixed_value_ok(self):
        new_states = [{"value": 1, "label": "Сеть", "severity": "normal"},
                      {"value": 2, "label": "GSM", "severity": "normal"}]
        current = self._power_type(id=5, code="net_type")
        self.db_mock.fetch_one.side_effect = [current, current, current]
        self.db_mock.fetch_all.return_value = [{"fixed_value": "2.0"}]
        response = self._put(5, {"states": new_states})
        self.assertEqual(response.code, 200)

    def test_put_numeric_states_float_fixed_value_missing_conflicts(self):
        new_states = [{"value": 0, "label": "A", "severity": "normal"},
                      {"value": 1, "label": "B", "severity": "normal"}]
        current = self._power_type(id=5, code="net_type")
        self.db_mock.fetch_one.side_effect = [current]
        self.db_mock.fetch_all.return_value = [{"fixed_value": "2.0"}]
        response = self._put(5, {"states": new_states})
        self.assertEqual(response.code, 409)
        self.assertEqual(json.loads(response.body)["detail"],
                         STATES_FIXED_VALUE_CONFLICT.format(values="2"))

    def test_put_unknown_type_returns_404(self):
        self.db_mock.fetch_one.return_value = None
        response = self._put(999, {"unit": "V"})
        self.assertEqual(response.code, 404)

    def test_put_unit_change_allowed(self):
        updated = dict(self.test_sensor_type, unit="K")
        self.db_mock.fetch_one.side_effect = [self.test_sensor_type, self.test_sensor_type,
                                              updated]
        response = self._put(1, {"unit": "K"})
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)["unit"], "K")

    def test_put_and_delete_power_no_longer_managed(self):
        updated = self._power_type(description="Напряжение питания", unit="V")
        self.db_mock.fetch_one.side_effect = [self._power_type(), self._power_type(), updated]
        response = self._put(4, {"description": "Напряжение питания", "unit": "V"})
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)["unit"], "V")

        self.db_mock.fetch_one.side_effect = [self._power_type(), {"id": 4}]
        response = self.fetch("/api/v1/sensor-types/4", method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 204)

