"""Unit-тесты handler'ов analytics.py: SensorAnalyticsHandler, LockerSummaryHandler.

Мокируется граница db.fetch_all/db.fetch_one (AnalyticsRepository делает сырые
SQL-запросы через AsyncPostgres, отдельного репозитория мокать негде и незачем —
см. test_media_handlers.py, тот же паттерн `make_app(db)` + AsyncHTTPTestCase).

Тесты — `def test_*` (нет pytest-asyncio в Astra); HTTP driven через
`tornado.testing.AsyncHTTPTestCase`, отдельный event loop на класс.
"""
from __future__ import annotations

from datetime import UTC, datetime
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app

_AUTH_HEADERS = {"X-Remote-User": "1", "X-Remote-Role": "operator"}


def _aggregation_row(sensor_id: str) -> dict:
    now = datetime.now(UTC)
    return {
        "id": 1,
        "locker_id": str(uuid4()),
        "sensor_id": sensor_id,
        "sensor_type_code": "temperature",
        "aggregation_type": "hourly",
        "period_start": now,
        "period_end": now,
        "value_avg": 21.5,
        "value_min": 20.0,
        "value_max": 23.0,
        "value_sum": 215.0,
        "value_stddev": 0.5,
        "sample_count": 10,
        "created_at": now,
    }


def _summary_row(sensor_id: str) -> dict:
    now = datetime.now(UTC)
    return {
        "sensor_id": sensor_id,
        "sensor_type_code": "temperature",
        "avg": 21.5,
        "min": 20.0,
        "max": 23.0,
        "sample_count": 10,
        "period_start": now,
        "period_end": now,
    }


class TestSensorAnalyticsHandler(AsyncHTTPTestCase):
    def get_app(self):
        self.db = MagicMock()
        self.db.fetch_all = AsyncMock(return_value=[])
        self.db.fetch_one = AsyncMock(return_value={"count": 0})
        return make_app(self.db)

    def test_missing_sensor_id_400(self):
        response = self.fetch("/api/v1/analytics/sensors", headers=_AUTH_HEADERS)
        self.assertEqual(response.code, 400)

    def test_invalid_aggregation_type_400(self):
        sensor_id = str(uuid4())
        response = self.fetch(
            f"/api/v1/analytics/sensors?sensor_id={sensor_id}&aggregation_type=weekly",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_invalid_metric_400(self):
        sensor_id = str(uuid4())
        response = self.fetch(
            f"/api/v1/analytics/sensors?sensor_id={sensor_id}&metric=median",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_invalid_from_date_400(self):
        sensor_id = str(uuid4())
        response = self.fetch(
            f"/api/v1/analytics/sensors?sensor_id={sensor_id}&from=not-a-date",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_invalid_to_date_400(self):
        sensor_id = str(uuid4())
        response = self.fetch(
            f"/api/v1/analytics/sensors?sensor_id={sensor_id}&to=not-a-date",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_success_returns_paginated_aggregations(self):
        sensor_id = str(uuid4())
        self.db.fetch_all = AsyncMock(return_value=[_aggregation_row(sensor_id)])
        self.db.fetch_one = AsyncMock(return_value={"count": 1})
        response = self.fetch(
            f"/api/v1/analytics/sensors?sensor_id={sensor_id}&aggregation_type=hourly",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["sensor_id"], sensor_id)

    def test_db_error_returns_500(self):
        sensor_id = str(uuid4())
        self.db.fetch_all = AsyncMock(side_effect=RuntimeError("db unavailable"))
        response = self.fetch(
            f"/api/v1/analytics/sensors?sensor_id={sensor_id}",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 500)


class TestLockerSummaryHandler(AsyncHTTPTestCase):
    def get_app(self):
        self.db = MagicMock()
        self.db.fetch_all = AsyncMock(return_value=[])
        return make_app(self.db)

    def test_invalid_aggregation_type_400(self):
        locker_id = str(uuid4())
        response = self.fetch(
            f"/api/v1/analytics/lockers/{locker_id}/summary?aggregation_type=weekly",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_invalid_from_date_400(self):
        locker_id = str(uuid4())
        response = self.fetch(
            f"/api/v1/analytics/lockers/{locker_id}/summary?from=not-a-date",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_invalid_to_date_400(self):
        locker_id = str(uuid4())
        response = self.fetch(
            f"/api/v1/analytics/lockers/{locker_id}/summary?to=not-a-date",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 400)

    def test_no_sensors_returns_404(self):
        locker_id = str(uuid4())
        self.db.fetch_all = AsyncMock(return_value=[])
        response = self.fetch(
            f"/api/v1/analytics/lockers/{locker_id}/summary",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 404)

    def test_success_returns_summary(self):
        locker_id = str(uuid4())
        sensor_id = str(uuid4())
        self.db.fetch_all = AsyncMock(return_value=[_summary_row(sensor_id)])
        response = self.fetch(
            f"/api/v1/analytics/lockers/{locker_id}/summary",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["locker_id"], locker_id)
        self.assertEqual(len(body["sensors"]), 1)
        self.assertEqual(body["sensors"][0]["sensor_id"], sensor_id)

    def test_db_error_returns_500(self):
        locker_id = str(uuid4())
        self.db.fetch_all = AsyncMock(side_effect=RuntimeError("db unavailable"))
        response = self.fetch(
            f"/api/v1/analytics/lockers/{locker_id}/summary",
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(response.code, 500)


if __name__ == "__main__":
    unittest.main()
