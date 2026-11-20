import logging
from typing import Any

from shared.database import AsyncPostgres

from ..locker_data.db.models.sensor import Sensor
from ..locker_data.db.models.sensor_type import SensorStateOption, SensorValueType
from ..locker_data.db.repositories.sensor import SensorRepository
from ..locker_data.db.repositories.sensor_type import SensorTypeRepository
from ..locker_data.db.repositories.threshold import (
    ThresholdRepository,
    normalize_boolean_fixed_value,
)
from ..sensor_states import ENUM_RANGE_THRESHOLD_ERROR, canonical_fixed_value
from .base import BaseHandler
from .dto import PaginatedResponse

logger = logging.getLogger(__name__)


async def _type_states(db: AsyncPostgres, sensor: Sensor) -> list[SensorStateOption] | None:
    sensor_type = await SensorTypeRepository(db).get_by_id(sensor.sensor_type_id)
    return sensor_type.states if sensor_type else None


def _validated_fixed_value(sensor: Sensor, states: list[SensorStateOption] | None,
                           min_value: Any, max_value: Any, fixed_value: Any) -> Any:
    """Проверить порог против states типа датчика и канонизировать fixed_value.

    Raises:
        ValueError: текст ошибки валидации для ответа 400.
    """
    if states is not None:
        if min_value is not None or max_value is not None:
            raise ValueError(ENUM_RANGE_THRESHOLD_ERROR)
        if fixed_value is not None and sensor.value_type != SensorValueType.BOOLEAN:
            return canonical_fixed_value(states, sensor.value_type, fixed_value)
    # Для boolean-датчика канонизируем fixed_value ("True"/"1" → "true"),
    # отвергая значения, которые никогда не совпадут с "true"/"false"
    # (иначе порог даёт вечное ложное нарушение).
    if fixed_value is not None and sensor.value_type == SensorValueType.BOOLEAN:
        return normalize_boolean_fixed_value(fixed_value)
    return fixed_value


class ThresholdListHandler(BaseHandler):
    """Handler для работы с пороговыми значениями датчика"""

    async def get(self, locker_id: str, sensor_id: str) -> None:
        """Получить пороговые значения для датчика с пагинацией."""
        sensor_repo = SensorRepository(self.db)
        sensor = await sensor_repo.get_by_id(sensor_id)

        if not sensor or sensor.locker_id != locker_id:
            self.write_not_found("Sensor not found")
            return

        params = self.parse_pagination_params(default_limit=100, max_limit=1000)
        if params is None:
            return
        limit, offset = params

        threshold_repo = ThresholdRepository(self.db)
        thresholds = await threshold_repo.get_by_sensor(sensor_id, limit=limit, offset=offset)
        total = await threshold_repo.count_by_sensor(sensor_id)

        self.write_json(
            PaginatedResponse(items=thresholds, total=total, limit=limit, offset=offset)
        )

    async def post(self, locker_id: str, sensor_id: str) -> None:
        """Создать новое пороговое значение для датчика"""
        body = self.get_json_body()

        min_value = body.get("min_value")
        max_value = body.get("max_value")
        fixed_value = body.get("fixed_value")
        description = body.get("description")

        # Проверяем, что указано хотя бы одно значение
        if min_value is None and max_value is None and fixed_value is None:
            self.write_validation_error("At least one of min_value, max_value, or fixed_value must be provided")
            return

        # Проверяем существование датчика
        sensor_repo = SensorRepository(self.db)
        sensor = await sensor_repo.get_by_id(sensor_id)

        if not sensor or sensor.locker_id != locker_id:
            self.write_not_found("Sensor not found")
            return

        try:
            fixed_value = _validated_fixed_value(
                sensor, await _type_states(self.db, sensor), min_value, max_value, fixed_value)
        except ValueError as exc:
            self.write_validation_error(str(exc))
            return

        # Создаем пороговое значение
        threshold_repo = ThresholdRepository(self.db)
        threshold = await threshold_repo.create(
            sensor_id=sensor_id,
            created_by=self.current_user_id,
            min_value=min_value,
            max_value=max_value,
            fixed_value=fixed_value,
            description=description,
        )

        self.write_json(threshold, status_code=201)


class ThresholdDetailHandler(BaseHandler):
    """Handler для работы с конкретным пороговым значением"""

    async def get(self, locker_id: str, sensor_id: str, threshold_id: str) -> None:
        """Получить информацию о пороговом значении"""
        threshold_repo = ThresholdRepository(self.db)
        threshold = await threshold_repo.get_by_id(int(threshold_id))

        if not threshold or threshold.sensor_id != sensor_id:
            self.write_not_found("Threshold not found")
            return

        # Проверяем, что сенсор принадлежит шкафу
        sensor_repo = SensorRepository(self.db)
        sensor = await sensor_repo.get_by_id(sensor_id)

        if not sensor or sensor.locker_id != locker_id:
            self.write_not_found("Sensor not found")
            return

        self.write_json(threshold)

    async def put(self, locker_id: str, sensor_id: str, threshold_id: str) -> None:
        """Обновить пороговое значение"""
        body = self.get_json_body()

        min_value = body.get("min_value")
        max_value = body.get("max_value")
        fixed_value = body.get("fixed_value")
        description = body.get("description")

        threshold_repo = ThresholdRepository(self.db)

        # Проверяем существование порогового значения
        threshold = await threshold_repo.get_by_id(int(threshold_id))
        if not threshold or threshold.sensor_id != sensor_id:
            self.write_not_found("Threshold not found")
            return

        user_id = self.current_user_id
        if user_id is None:
            self.write_unauthorized("authentication required")
            return

        # Тот же инвариант, что и при создании.
        if min_value is not None or max_value is not None or fixed_value is not None:
            sensor_repo = SensorRepository(self.db)
            sensor = await sensor_repo.get_by_id(sensor_id)
            if not sensor or sensor.locker_id != locker_id:
                self.write_not_found("Sensor not found")
                return
            try:
                fixed_value = _validated_fixed_value(
                    sensor, await _type_states(self.db, sensor), min_value, max_value, fixed_value)
            except ValueError as exc:
                self.write_validation_error(str(exc))
                return

        updated_threshold = await threshold_repo.update(
            threshold_id=int(threshold_id),
            updated_by=user_id,
            min_value=min_value,
            max_value=max_value,
            fixed_value=fixed_value,
            description=description,
        )

        self.write_json(updated_threshold)

    async def delete(self, locker_id: str, sensor_id: str, threshold_id: str) -> None:
        """Удалить пороговое значение"""
        threshold_repo = ThresholdRepository(self.db)

        # Проверяем существование порогового значения
        threshold = await threshold_repo.get_by_id(int(threshold_id))
        if not threshold or threshold.sensor_id != sensor_id:
            self.write_not_found("Threshold not found")
            return

        success = await threshold_repo.delete(int(threshold_id))

        if not success:
            self.write_not_found("Threshold not found")
            return

        self.set_status(204)
        self.finish()
