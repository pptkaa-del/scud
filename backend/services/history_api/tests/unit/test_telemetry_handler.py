"""Unit-тесты handler'ов telemetry.py: TelemetryBatchHandler, TelemetryListHandler.

Мокируется граница db.fetch_all/db.fetch_one (TelemetryRepository — сырые SQL
через AsyncPostgres). Паттерн — `make_app(db)` + AsyncHTTPTestCase, см.
test_media_handlers.py.

Тесты — `def test_*` (нет pytest-asyncio в Astra).
"""
from __future__ import annotations

from datetime import UTC, datetime
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app

_AUTH_HEADERS = {
    "X-Remote-User": "1",
    "X-Remote-Role": "operator",
    "Content-Type": "application/json",
}


def _telemetry_row(sensor_id: str, locker_id: str) -> dict:
    return {
        "id": 1,
        "locker_id": locker_id,
        "sensor_id": sensor_id,
        "sensor_type_code": "temperature",
        "value": 21.5,
        "value_text": None,
        "recorded_at": datetime.now(UTC),
        "metadata": {},
    }


class TestTelemetryBatchHandler(AsyncHTTPTestCase):
    def get_app(self):
        self.db = MagicMock()
        # insert_batch делает fetch_all с RETURNING — по строке на вставку.
        self.db.fetch_all = AsyncMock(return_value=[{"?column?": 1}])
        return make_app(self.db)

    def _post(self, payload: dict):
        return self.fetch(
            "/api/v1/telemetry",
            method="POST",
            body=json.dumps(payload).encode("utf-8"),
            headers=_AUTH_HEADERS,
        )

    def test_success_returns_201_with_inserted_count(self):
        locker_id = str(uuid4())
        sensor_id = str(uuid4())
        self.db.fetch_all = AsyncMock(return_value=[{"?column?": 1}, {"?column?": 1}])
        response = self._post({
            "locker_id": locker_id,
            "readings": [
                {"sensor_id": sensor_id, "sensor_type_code": "temperature", "value": 21.5},
                {"sensor_id": sensor_id, "sensor_type_code": "humidity", "value": 40.0},
            ],
        })
        self.assertEqual(response.code, 201)
        body = json.loads(response.body)
        self.assertEqual(body["inserted"], 2)

    def test_empty_readings_400(self):
        response = self._post({"readings": []})
        self.assertEqual(response.code, 400)

    def test_too_many_readings_400(self):
        sensor_id = str(uuid4())
        readings = [
            {"sensor_id": sensor_id, "sensor_type_code": "temperature", "value": 1.0}
            for _ in range(1001)
        ]
        response = self._post({"readings": readings})
        self.assertEqual(response.code, 400)

    def test_missing_sensor_id_400(self):
        response = self._post({"readings": [{"sensor_type_code": "temperature"}]})
        self.assertEqual(response.code, 400)

    def test_missing_sensor_type_code_400(self):
        sensor_id = str(uuid4())
        response = self._post({"readings": [{"sensor_id": sensor_id}]})
        self.assertEqual(response.code, 400)

    def test_invalid_recorded_at_400(self):
        sensor_id = str(uuid4())
        response = self._post({
            "readings": [{
                "sensor_id": sensor_id,
                "sensor_type_code": "temperature",
                "recorded_at": "not-a-date",
            }],
        })
        self.assertEqual(response.code, 400)

    def test_db_error_returns_500(self):
        sensor_id = str(uuid4())
        self.db.fetch_all = AsyncMock(side_effect=RuntimeError("db unavailable"))
        response = self._post({
            "readings": [{"sensor_id": sensor_id, "sensor_type_code": "temperature", "value": 1.0}],
        })
        self.assertEqual(response.code, 500)


class TestTelemetryListHandler(AsyncHTTPTestCase):
    def get_app(self):
        self.db = MagicMock()
        self.db.fetch_all = AsyncMock(return_value=[])
        self.db.fetch_one = AsyncMock(return_value={"count": 0})
        return make_app(self.db)

    def test_success_returns_paginated_telemetry(self):
        locker_id = str(uuid4())
        sensor_id = str(uuid4())
        self.db.fetch_all = AsyncMock(return_value=[_telemetry_row(sensor_id, locker_id)])
        self.db.fetch_one = AsyncMock(return_value={"count": 1})
        response = self.fetch(
            f"/api/v1/telemetry/list?locker_id={locker_id}&sensor_id={sensor_id}",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["sensor_id"], sensor_id)

    def test_invalid_limit_400(self):
        response = self.fetch(
            "/api/v1/telemetry/list?limit=0",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_limit_over_max_400(self):
        response = self.fetch(
            "/api/v1/telemetry/list?limit=10001",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_invalid_from_date_400(self):
        response = self.fetch(
            "/api/v1/telemetry/list?from=not-a-date",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_invalid_to_date_400(self):
        response = self.fetch(
            "/api/v1/telemetry/list?to=not-a-date",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_db_error_returns_500(self):
        self.db.fetch_all = AsyncMock(side_effect=RuntimeError("db unavailable"))
        response = self.fetch("/api/v1/telemetry/list", headers=_AUTH_HEADERS)
        self.assertEqual(response.code, 500)


if __name__ == "__main__":
    unittest.main()
