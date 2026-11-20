from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AggregationType(StrEnum):
    """Тип временной агрегации"""
    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"


@dataclass(slots=True)
class TelemetryAggregation:
    """
    Предрасчитанный агрегат телеметрии.

    Хранит агрегированные данные по датчикам для ускорения аналитических запросов.
    Агрегаты рассчитываются фоновым сервисом.

    Attributes:
        id: ID записи (BIGSERIAL)
        locker_id: UUID шкафа
        sensor_id: UUID датчика
        sensor_type_code: Код типа датчика
        aggregation_type: Тип агрегации (hourly/daily/monthly)
        period_start: Начало периода агрегации
        period_end: Конец периода агрегации
        value_avg: Среднее значение за период
        value_min: Минимальное значение за период
        value_max: Максимальное значение за период
        value_sum: Сумма значений за период
        value_stddev: Стандартное отклонение
        sample_count: Количество сэмплов в агрегате
        created_at: Время создания агрегата
    """
    id: int
    locker_id: str  # UUID
    sensor_id: str  # UUID
    sensor_type_code: str
    aggregation_type: AggregationType
    period_start: datetime
    period_end: datetime
    value_avg: float | None
    value_min: float | None
    value_max: float | None
    value_sum: float | None
    value_stddev: float | None
    sample_count: int
    created_at: datetime
