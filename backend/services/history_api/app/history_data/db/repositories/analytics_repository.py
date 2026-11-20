from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from dacite import Config, from_dict

from shared.database import AsyncPostgres, SqlParam

from ..models.telemetry_aggregation import AggregationType, TelemetryAggregation

_DACITE_CONFIG = Config(cast=[AggregationType])


class AnalyticsRepository:
    """
    Репозиторий для работы с аналитическими данными.

    Обеспечивает доступ к предрасчитанным агрегатам телеметрии
    и сводной аналитике по шкафам.
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_sensor_aggregations(
        self,
        sensor_id: str,
        aggregation_type: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[TelemetryAggregation]:
        """
        Получить агрегированные данные датчика с пагинацией.

        Args:
            sensor_id: UUID датчика (обязательно)
            aggregation_type: Тип агрегации ('hourly', 'daily', 'monthly')
            from_date: Начало временного диапазона
            to_date: Конец временного диапазона
            limit: Максимальное количество записей
            offset: Смещение

        Returns:
            Список агрегатов телеметрии (текущая страница).
        """
        query = "SELECT * FROM telemetry_aggregations WHERE sensor_id = %s"
        params: list[SqlParam] = [sensor_id]

        if aggregation_type:
            query += " AND aggregation_type = %s"
            params.append(aggregation_type)

        if from_date:
            query += " AND period_start >= %s"
            params.append(from_date)

        if to_date:
            query += " AND period_end <= %s"
            params.append(to_date)

        query += " ORDER BY period_start DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = await self._db.fetch_all(query, tuple(params))
        return [from_dict(TelemetryAggregation, row, config=_DACITE_CONFIG) for row in rows]

    async def get_aggregations_count(
        self,
        sensor_id: str,
        aggregation_type: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None
    ) -> int:
        """
        Получить количество агрегатов для датчика.

        Args:
            sensor_id: UUID датчика
            aggregation_type: Тип агрегации
            from_date: Начало временного диапазона
            to_date: Конец временного диапазона

        Returns:
            Количество агрегатов
        """
        query = "SELECT COUNT(*) as count FROM telemetry_aggregations WHERE sensor_id = %s"
        params: list[SqlParam] = [sensor_id]

        if aggregation_type:
            query += " AND aggregation_type = %s"
            params.append(aggregation_type)

        if from_date:
            query += " AND period_start >= %s"
            params.append(from_date)

        if to_date:
            query += " AND period_end <= %s"
            params.append(to_date)

        row = await self._db.fetch_one(query, tuple(params))
        return row['count'] if row else 0

    DEFAULT_SUMMARY_WINDOW_DAYS = 30

    async def get_locker_summary(
        self,
        locker_id: str,
        aggregation_type: str | None = None,  # noqa: ARG002 — часть публичного контракта summary endpoint, см. openapi.yaml
        from_date: datetime | None = None,
        to_date: datetime | None = None
    ) -> dict[str, Any]:
        """
        Получить сводную аналитику по всем датчикам шкафа.

        Если ни ``from_date``, ни ``to_date`` не заданы, неявно ограничивается
        последними DEFAULT_SUMMARY_WINDOW_DAYS днями. Без этого ограничения
        агрегация принудительно сканирует все ~36+ месячных партиций
        (десятки миллионов строк) — никакая партиционная подрезка не работает.

        Args:
            locker_id: UUID шкафа
            aggregation_type: Тип агрегации ('hourly', 'daily', 'monthly')
            from_date: Начало временного диапазона
            to_date: Конец временного диапазона

        Returns:
            Словарь с сводной аналитикой:
            {
                "locker_id": "uuid",
                "period_start": "datetime",
                "period_end": "datetime",
                "sensors": [
                    {
                        "sensor_id": "uuid",
                        "sensor_type_code": "temperature",
                        "avg": 24.5,
                        "min": 20.0,
                        "max": 30.0,
                        "sample_count": 100
                    },
                    ...
                ]
            }
        """
        # Один SELECT вместо двух: per-sensor агрегаты + период покрытия.
        # MIN/MAX(recorded_at) считается один раз и тиражируется по всем строкам
        # через оконные функции — второй проход по партициям не нужен.
        query = """
            SELECT
                sensor_id,
                sensor_type_code,
                AVG(value) as avg,
                MIN(value) as min,
                MAX(value) as max,
                COUNT(*) as sample_count,
                MIN(MIN(recorded_at)) OVER () AS period_start,
                MAX(MAX(recorded_at)) OVER () AS period_end
            FROM sensor_telemetry
            WHERE locker_id = %s
        """
        params: list[SqlParam] = [locker_id]

        # Принуждаем хоть какой-то верхний/нижний bound, чтобы partition pruning
        # отсекал старые партиции. Если caller совсем без диапазона —
        # подразумеваем последние DEFAULT_SUMMARY_WINDOW_DAYS дней.
        if from_date is None and to_date is None:
            from_date = datetime.now(UTC) - timedelta(days=self.DEFAULT_SUMMARY_WINDOW_DAYS)

        if from_date:
            query += " AND recorded_at >= %s"
            params.append(from_date)

        if to_date:
            query += " AND recorded_at <= %s"
            params.append(to_date)

        query += " GROUP BY sensor_id, sensor_type_code ORDER BY sensor_type_code"

        rows = await self._db.fetch_all(query, tuple(params))

        sensors = []
        period_start = None
        period_end = None
        for row in rows:
            sensors.append({
                "sensor_id": row['sensor_id'],
                "sensor_type_code": row['sensor_type_code'],
                "avg": float(row['avg']) if row['avg'] is not None else None,
                "min": float(row['min']) if row['min'] is not None else None,
                "max": float(row['max']) if row['max'] is not None else None,
                "sample_count": int(row['sample_count']) if row['sample_count'] else 0
            })
            # Window function tiles per row — берём из любого row.
            if period_start is None:
                period_start = row.get('period_start')
                period_end = row.get('period_end')

        return {
            "locker_id": locker_id,
            "period_start": period_start,
            "period_end": period_end,
            "sensors": sensors
        }
