from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging

from shared.database import AsyncPostgres

logger = logging.getLogger(__name__)

_UPSERT_AGGREGATION_SQL = """
    INSERT INTO telemetry_aggregations (
        locker_id, sensor_id, sensor_type_code,
        aggregation_type, period_start, period_end,
        value_avg, value_min, value_max, value_sum, value_stddev,
        sample_count
    )
    SELECT
        locker_id,
        sensor_id,
        sensor_type_code,
        %s::aggregation_type,
        %s::timestamptz,
        %s::timestamptz,
        AVG(value),
        MIN(value),
        MAX(value),
        SUM(value),
        STDDEV(value),
        COUNT(*)
    FROM sensor_telemetry
    WHERE recorded_at >= %s
      AND recorded_at < %s
      AND value IS NOT NULL
    GROUP BY locker_id, sensor_id, sensor_type_code
    ON CONFLICT (sensor_id, aggregation_type, period_start)
    DO UPDATE SET
        value_avg    = EXCLUDED.value_avg,
        value_min    = EXCLUDED.value_min,
        value_max    = EXCLUDED.value_max,
        value_sum    = EXCLUDED.value_sum,
        value_stddev = EXCLUDED.value_stddev,
        sample_count = EXCLUDED.sample_count
"""


class AggregationService:
    """
    Фоновый сервис расчёта агрегатов телеметрии.

    Периодически рассчитывает часовые, дневные и месячные агрегаты
    из таблицы sensor_telemetry и сохраняет результаты
    в таблицу telemetry_aggregations (upsert по уникальному индексу).

    Паттерн аналогичен NotificationScheduler в notification_api.
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db
        self._running = False

    async def start(self, interval: int = 300) -> None:
        """Запустить фоновый процесс агрегации"""
        self._running = True
        logger.info("Aggregation service started (interval=%ds)", interval)

        while self._running:
            try:
                await self._process_hourly()
                await self._process_daily()
                await self._process_monthly()
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error("Error in aggregation service: %s", e, exc_info=True)
                await asyncio.sleep(interval)

        logger.info("Aggregation service stopped")

    def stop(self) -> None:
        """Остановить фоновый процесс агрегации"""
        self._running = False

    # ------------------------------------------------------------------
    # Вычисление границ периода
    # ------------------------------------------------------------------

    @staticmethod
    def _get_period_boundaries(
        aggregation_type: str,
    ) -> tuple[datetime, datetime]:
        """
        Вернуть (period_start, period_end) для последнего завершённого периода
        относительно текущего момента UTC.

        Примеры (UTC now = 2026-04-12 14:35):
        - hourly  → (13:00, 14:00)
        - daily   → (2026-04-11 00:00, 2026-04-12 00:00)
        - monthly → (2026-03-01 00:00, 2026-04-01 00:00)
        """
        now = datetime.now(UTC)

        if aggregation_type == "hourly":
            period_end = now.replace(minute=0, second=0, microsecond=0)
            period_start = period_end - timedelta(hours=1)

        elif aggregation_type == "daily":
            current_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            period_end = current_day
            period_start = current_day - timedelta(days=1)

        elif aggregation_type == "monthly":
            first_of_month = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            period_end = first_of_month
            if first_of_month.month == 1:
                period_start = first_of_month.replace(
                    year=first_of_month.year - 1, month=12
                )
            else:
                period_start = first_of_month.replace(
                    month=first_of_month.month - 1
                )

        else:
            raise ValueError(f"Unknown aggregation type: {aggregation_type}")

        return period_start, period_end

    # ------------------------------------------------------------------
    # Обработка каждого типа агрегации
    # ------------------------------------------------------------------

    async def _process_hourly(self) -> None:
        """Рассчитать часовые агрегаты за последний завершённый час."""
        await self._aggregate("hourly")

    async def _process_daily(self) -> None:
        """Рассчитать дневные агрегаты за последние завершённые сутки."""
        await self._aggregate("daily")

    async def _process_monthly(self) -> None:
        """Рассчитать месячные агрегаты за последний завершённый месяц."""
        await self._aggregate("monthly")

    async def _aggregate(self, aggregation_type: str) -> None:
        """
        Выполнить upsert агрегатов указанного типа за последний
        завершённый период.
        """
        period_start, period_end = self._get_period_boundaries(aggregation_type)

        await self._db.execute(
            _UPSERT_AGGREGATION_SQL,
            [aggregation_type, period_start, period_end,
             period_start, period_end],
        )

        logger.info(
            "Aggregation [%s] %s..%s — upsert executed",
            aggregation_type,
            period_start.isoformat(),
            period_end.isoformat(),
        )
