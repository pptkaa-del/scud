"""Unit-тесты для AnalyticsRepository.get_locker_summary (H-3: partition pruning).

Без явного диапазона дат get_locker_summary() раньше сканировал все
партиции sensor_telemetry (COUNT/AVG/MIN/MAX по десяткам миллионов строк).
Зеркалит уже принятый фикс TelemetryRepository.get_count()
(DEFAULT_COUNT_WINDOW_DAYS, backend/services/history_api/app/history_data/db/
repositories/telemetry_repository.py:146-178): если ни from_date, ни to_date
не заданы, подставляется нижняя граница recorded_at >= now() -
DEFAULT_SUMMARY_WINDOW_DAYS, чтобы partition pruning отсекал старые партиции.

Мокаем db.fetch_all и инспектируем перехваченный SQL + параметры.
Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import AsyncMock

from app.history_data.db.repositories.analytics_repository import AnalyticsRepository

_FROM_DATE = datetime.fromisoformat("2026-06-01T00:00:00+00:00")
_TO_DATE = datetime.fromisoformat("2026-06-30T23:59:59+00:00")


def _db() -> AsyncMock:
    db = AsyncMock()
    db.fetch_all = AsyncMock(return_value=[])
    return db


class TestGetLockerSummaryDefaultWindow(unittest.TestCase):
    def test_no_dates_defaults_to_recorded_at_lower_bound(self):
        """Без from_date/to_date должна появиться нижняя граница recorded_at,
        обеспечивающая partition pruning (H-3)."""
        before = datetime.now(UTC) - timedelta(days=AnalyticsRepository.DEFAULT_SUMMARY_WINDOW_DAYS)

        async def run() -> AsyncMock:
            db = _db()
            await AnalyticsRepository(db).get_locker_summary(locker_id="locker-1")
            return db

        db = asyncio.run(run())
        sql, params = db.fetch_all.call_args[0]

        self.assertIn("AND recorded_at >= %s", sql)
        # Нижняя граница — это datetime, а не locker_id/сентинел.
        bound_params = [p for p in params if isinstance(p, datetime)]
        self.assertEqual(len(bound_params), 1)
        lower_bound = bound_params[0]

        after = datetime.now(UTC) - timedelta(days=AnalyticsRepository.DEFAULT_SUMMARY_WINDOW_DAYS)
        self.assertGreaterEqual(lower_bound, before)
        self.assertLessEqual(lower_bound, after)

    def test_default_window_is_thirty_days(self):
        self.assertEqual(AnalyticsRepository.DEFAULT_SUMMARY_WINDOW_DAYS, 30)

    def test_explicit_range_preserved_unchanged(self):
        """Если caller явно передал диапазон — поведение не меняется:
        используются ИМЕННО переданные from_date/to_date, а не default-окно."""
        async def run() -> AsyncMock:
            db = _db()
            await AnalyticsRepository(db).get_locker_summary(
                locker_id="locker-1", from_date=_FROM_DATE, to_date=_TO_DATE
            )
            return db

        db = asyncio.run(run())
        sql, params = db.fetch_all.call_args[0]

        self.assertIn("AND recorded_at >= %s", sql)
        self.assertIn("AND recorded_at <= %s", sql)
        self.assertIn(_FROM_DATE, params)
        self.assertIn(_TO_DATE, params)
        # Ровно два datetime-параметра — переданные, без подмешанного default.
        bound_params = [p for p in params if isinstance(p, datetime)]
        self.assertEqual(bound_params, [_FROM_DATE, _TO_DATE])

    def test_only_from_date_does_not_trigger_default(self):
        """Один из двух дат уже задан caller'ом — default-окно не подставляется,
        to_date остаётся неограниченным (как раньше)."""
        async def run() -> AsyncMock:
            db = _db()
            await AnalyticsRepository(db).get_locker_summary(
                locker_id="locker-1", from_date=_FROM_DATE
            )
            return db

        db = asyncio.run(run())
        sql, params = db.fetch_all.call_args[0]

        self.assertIn("AND recorded_at >= %s", sql)
        self.assertNotIn("AND recorded_at <= %s", sql)
        self.assertEqual(tuple(params), ("locker-1", _FROM_DATE))


if __name__ == "__main__":
    unittest.main()
