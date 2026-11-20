from __future__ import annotations

from datetime import datetime
import logging

from ..history_data.db.repositories.analytics_repository import AnalyticsRepository
from .base import BaseHandler
from .dto import PaginatedResponse

logger = logging.getLogger(__name__)


class SensorAnalyticsHandler(BaseHandler):
    """
    GET /api/v1/analytics/sensors - Получить агрегированные данные датчиков
    """

    async def get(self) -> None:
        """Получить агрегированные данные датчиков с фильтрацией и пагинацией."""
        try:
            sensor_id = self.get_query_argument("sensor_id", default=None)
            aggregation_type = self.get_query_argument("aggregation_type", default=None)
            from_str = self.get_query_argument("from", default=None)
            to_str = self.get_query_argument("to", default=None)

            if not sensor_id:
                self.write_validation_error("Missing required parameter 'sensor_id'")
                return

            params = self.parse_pagination_params(default_limit=1000, max_limit=10000)
            if params is None:
                return
            limit, offset = params

            if aggregation_type and aggregation_type not in ['hourly', 'daily', 'monthly']:
                self.write_validation_error("Invalid aggregation_type. Must be one of: hourly, daily, monthly")
                return

            metric = self.get_query_argument("metric", default=None)
            if metric and metric not in ['avg', 'min', 'max', 'sum', 'stddev']:
                self.write_validation_error("Invalid metric. Must be one of: avg, min, max, sum, stddev")
                return

            from_date: datetime | None = None
            to_date: datetime | None = None

            if from_str:
                try:
                    from_date = datetime.fromisoformat(from_str)
                except ValueError:
                    self.write_validation_error("Invalid 'from' date format (use ISO 8601)")
                    return

            if to_str:
                try:
                    to_date = datetime.fromisoformat(to_str)
                except ValueError:
                    self.write_validation_error("Invalid 'to' date format (use ISO 8601)")
                    return

            repo = AnalyticsRepository(self.db)
            aggregations = await repo.get_sensor_aggregations(
                sensor_id=sensor_id,
                aggregation_type=aggregation_type,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
                offset=offset,
            )
            total_count = await repo.get_aggregations_count(
                sensor_id=sensor_id,
                aggregation_type=aggregation_type,
                from_date=from_date,
                to_date=to_date,
            )

            self.write_json(
                PaginatedResponse(
                    items=aggregations,
                    total=total_count,
                    limit=limit,
                    offset=offset,
                )
            )

        except Exception as e:
            logger.error(f"Error fetching sensor analytics: {e}", exc_info=True)
            self.write_json({
                "error": "Internal server error",
                "details": str(e)
            }, status_code=500)


class LockerSummaryHandler(BaseHandler):
    """
    GET /api/v1/analytics/lockers/{lockerId}/summary - Сводка по шкафу
    """

    async def get(self, locker_id: str) -> None:
        """Получить сводную аналитику по всем датчикам шкафа"""
        try:
            # Извлечение query параметров
            aggregation_type = self.get_query_argument("aggregation_type", default=None)
            from_str = self.get_query_argument("from", default=None)
            to_str = self.get_query_argument("to", default=None)

            # Валидация aggregation_type
            if aggregation_type and aggregation_type not in ['hourly', 'daily', 'monthly']:
                self.write_validation_error("Invalid aggregation_type. Must be one of: hourly, daily, monthly")
                return

            # Парсинг дат
            from_date: datetime | None = None
            to_date: datetime | None = None

            if from_str:
                try:
                    from_date = datetime.fromisoformat(from_str)
                except ValueError:
                    self.write_validation_error("Invalid 'from' date format (use ISO 8601)")
                    return

            if to_str:
                try:
                    to_date = datetime.fromisoformat(to_str)
                except ValueError:
                    self.write_validation_error("Invalid 'to' date format (use ISO 8601)")
                    return

            # Получение данных из БД
            repo = AnalyticsRepository(self.db)
            summary = await repo.get_locker_summary(
                locker_id=locker_id,
                aggregation_type=aggregation_type,
                from_date=from_date,
                to_date=to_date
            )

            # Проверка существования шкафа (если нет датчиков, значит шкаф не существует)
            if not summary.get("sensors"):
                self.write_not_found("Locker not found")
                return

            self.write_json(summary)

        except Exception as e:
            logger.error(f"Error fetching locker summary: {e}", exc_info=True)
            self.write_json({
                "error": "Internal server error",
                "details": str(e)
            }, status_code=500)
