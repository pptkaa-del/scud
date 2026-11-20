"""Unit-тесты SensorRepository.update_values (C-3).

Раньше телеметрический батч на N датчиков делал N отдельных
`update_value` round-trip'ов в цикле `_handle_telemetry` (N+1 UPDATE per
batch). `update_values` заменяет это единственным UPDATE ... FROM
unnest(...) statement'ом. Здесь проверяется репозиторий изолированно —
`self._db` замокан, живой Postgres не нужен.
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.locker_data.db.repositories.sensor import SensorRepository


class TestSensorRepositoryUpdateValuesBatching(unittest.TestCase):
    """(a) N датчиков в батче -> ровно один вызов db.execute (не N)."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.execute = AsyncMock()
        self.repo = SensorRepository(self.db_mock)

    def test_batch_of_n_readings_issues_single_execute_call(self):
        updates = [
            ("sensor-1", 1.5),
            ("sensor-2", True),
            ("sensor-3", "label"),
            ("sensor-4", False),
            ("sensor-5", 42.0),
        ]

        asyncio.run(self.repo.update_values(updates))

        self.assertEqual(self.db_mock.execute.call_count, 1)

    def test_single_update_is_also_a_single_execute_call(self):
        asyncio.run(self.repo.update_values([("sensor-1", 1.0)]))

        self.assertEqual(self.db_mock.execute.call_count, 1)

    def test_empty_updates_is_noop_no_db_call(self):
        asyncio.run(self.repo.update_values([]))

        self.db_mock.execute.assert_not_called()


class TestSensorRepositoryUpdateValuesSql(unittest.TestCase):
    """(b) сгенерированный SQL использует unnest и корректно биндит id/val."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.execute = AsyncMock()
        self.repo = SensorRepository(self.db_mock)

    def test_sql_uses_unnest_with_uuid_and_jsonb_arrays(self):
        asyncio.run(self.repo.update_values([("sensor-1", 1.5), ("sensor-2", False)]))

        sql = self.db_mock.execute.call_args[0][0]
        self.assertIn("UPDATE sensors", sql)
        self.assertIn("FROM unnest(%s::uuid[], %s::jsonb[]) AS v(id, val)", sql)
        self.assertIn("current_value = v.val::jsonb", sql)
        self.assertIn("updated_at = now()", sql)
        self.assertIn("WHERE sensors.id = v.id", sql)

    def test_binds_parallel_id_and_val_arrays_in_order(self):
        updates = [
            ("sensor-1", 1.5),
            ("sensor-2", True),
            ("sensor-3", "label"),
        ]

        asyncio.run(self.repo.update_values(updates))

        params = self.db_mock.execute.call_args[0][1]
        ids, values = params
        self.assertEqual(ids, ["sensor-1", "sensor-2", "sensor-3"])
        self.assertEqual(len(values), 3)
        # Сериализация идентична update_value: JSONB-обёртка {"v": value}
        # (_pack_value), кодированная в JSON-строку под явный ::jsonb cast.
        self.assertEqual(json.loads(values[0]), {"v": 1.5})
        self.assertEqual(json.loads(values[1]), {"v": True})
        self.assertEqual(json.loads(values[2]), {"v": "label"})

    def test_none_value_serialized_as_null_not_wrapped(self):
        asyncio.run(self.repo.update_values([("sensor-1", None)]))

        _ids, values = self.db_mock.execute.call_args[0][1]
        self.assertIsNone(values[0])

    def test_sensor_ids_are_stringified(self):
        """sensor_id из wire-протокола может прийти не строкой; массив id
        для ::uuid[] должен получить str(), как это уже делает get_type_info."""
        asyncio.run(self.repo.update_values([(123, 1.0)]))

        ids, _values = self.db_mock.execute.call_args[0][1]
        self.assertEqual(ids, ["123"])
        self.assertIsInstance(ids[0], str)


if __name__ == "__main__":
    unittest.main()
