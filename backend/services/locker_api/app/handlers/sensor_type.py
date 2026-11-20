"""
HTTP handlers для управления типами датчиков (sensor_types).
"""

import logging
from typing import Final

from shared.database import AsyncPostgres, is_foreign_key_violation

from ..locker_data.db.models.sensor_type import SensorStateOption, SensorValueType
from ..locker_data.db.repositories.sensor_type import (
    KEEP_STATES,
    SensorTypeRepository,
    StatesUpdate,
)
from ..locker_data.db.repositories.threshold import ThresholdRepository
from ..sensor_states import fixed_value_key, parse_states, states_to_raw
from .base import BaseHandler
from .dto import PaginatedResponse

logger = logging.getLogger(__name__)

STATES_RANGE_CONFLICT: Final = (
    "У датчиков этого типа есть пороги min/max — удалите их перед заданием states"
)
STATES_FIXED_VALUE_CONFLICT: Final = (
    "Пороги датчиков этого типа используют значения {values}, которых нет в новых states"
)


async def _states_conflict(
    db: AsyncPostgres,
    type_id: int,
    value_type: str,
    states: list[SensorStateOption],
    *,
    becomes_enum: bool,
) -> str | None:
    thresholds = ThresholdRepository(db)
    if becomes_enum and await thresholds.count_active_range_by_type(type_id):
        return STATES_RANGE_CONFLICT
    allowed = {fixed_value_key(s.value, value_type) for s in states}
    missing = await thresholds.active_fixed_values_by_type(type_id, value_type) - allowed
    if missing:
        return STATES_FIXED_VALUE_CONFLICT.format(values=", ".join(sorted(missing)))
    return None


def _parse_value_type(value: str) -> SensorValueType | None:
    try:
        return SensorValueType(value)
    except ValueError:
        return None


def _value_type_error_message() -> str:
    allowed = ", ".join(vt.value for vt in SensorValueType)
    return f"value_type must be one of: {allowed}"


class SensorTypeListHandler(BaseHandler):
    """Handler для работы со списком типов датчиков"""

    async def get(self) -> None:
        """Получить страницу типов датчиков (всего обычно ~10 штук)."""
        params = self.parse_pagination_params(default_limit=100, max_limit=1000)
        if params is None:
            return
        limit, offset = params

        repo = SensorTypeRepository(self.db)
        sensor_types = await repo.get_all(limit=limit, offset=offset)
        total = await repo.count()

        self.write_json(
            PaginatedResponse(items=sensor_types, total=total, limit=limit, offset=offset)
        )

    async def post(self) -> None:
        """Создать новый тип датчика"""
        body = self.get_json_body()

        code = body.get("code")
        if not code:
            self.write_validation_error("code is required")
            return

        name = body.get("name")
        if not name:
            self.write_validation_error("name is required")
            return

        value_type_raw = body.get("value_type")
        if not value_type_raw:
            self.write_validation_error("value_type is required")
            return

        value_type_enum = _parse_value_type(value_type_raw)
        if value_type_enum is None:
            self.write_validation_error(_value_type_error_message())
            return
        value_type = value_type_enum.value

        description = body.get("description")
        unit = body.get("unit")

        states: list[SensorStateOption] | None = None
        if body.get("states") is not None:
            try:
                states = parse_states(body["states"], value_type)
            except ValueError as exc:
                self.write_validation_error(str(exc))
                return

        repo = SensorTypeRepository(self.db)

        existing = await repo.get_by_code(code)
        if existing:
            self.write_conflict(f"Sensor type with code '{code}' already exists")
            return

        sensor_type = await repo.create(
            code=code,
            name=name,
            value_type=value_type,
            description=description,
            unit=unit,
            states=states,
        )

        self.write_json(sensor_type, status_code=201)


class SensorTypeDetailHandler(BaseHandler):
    """Handler для работы с конкретным типом датчика"""

    async def get(self, type_id: str) -> None:
        """Получить информацию о типе датчика"""
        type_id_int = self.get_int_path_arg(type_id, "type_id")

        repo = SensorTypeRepository(self.db)
        sensor_type = await repo.get_by_id(type_id_int)

        if not sensor_type:
            self.write_not_found("Sensor type not found")
            return

        self.write_json(sensor_type)

    async def put(self, type_id: str) -> None:
        """Обновить тип датчика"""
        type_id_int = self.get_int_path_arg(type_id, "type_id")

        body = self.get_json_body()

        name = body.get("name")
        description = body.get("description")
        unit = body.get("unit")
        value_type_raw = body.get("value_type")
        value_type = None
        if value_type_raw:
            value_type_enum = _parse_value_type(value_type_raw)
            if value_type_enum is None:
                self.write_validation_error(_value_type_error_message())
                return
            value_type = value_type_enum.value

        repo = SensorTypeRepository(self.db)

        current = await repo.get_by_id(type_id_int)
        if not current:
            self.write_not_found("Sensor type not found")
            return

        new_value_type = value_type or current.value_type
        states: list[SensorStateOption] | None | StatesUpdate = KEEP_STATES
        try:
            if "states" in body:
                raw = body["states"]
                states = None if raw is None else parse_states(raw, new_value_type)
            elif current.states is not None and new_value_type != current.value_type:
                parse_states(states_to_raw(current.states), new_value_type)
        except ValueError as exc:
            self.write_validation_error(str(exc))
            return
        if isinstance(states, list):
            conflict = await _states_conflict(
                self.db, type_id_int, new_value_type, states,
                becomes_enum=current.states is None,
            )
            if conflict:
                self.write_conflict(conflict)
                return

        updated_sensor_type = await repo.update(
            type_id=type_id_int,
            name=name,
            description=description,
            unit=unit,
            value_type=value_type,
            states=states,
        )

        if not updated_sensor_type:
            self.write_not_found("Sensor type not found")
            return

        self.write_json(updated_sensor_type)

    async def delete(self, type_id: str) -> None:
        """Удалить тип датчика"""
        type_id_int = self.get_int_path_arg(type_id, "type_id")

        repo = SensorTypeRepository(self.db)

        # Проверяем существование
        sensor_type = await repo.get_by_id(type_id_int)
        if not sensor_type:
            self.write_not_found("Sensor type not found")
            return

        try:
            success = await repo.delete(type_id_int)
        except Exception as exc:
            # На type_id ссылаются sensors (FK) → psycopg2 поднимает 23503.
            # Это конфликт состояния ресурса (409), а не серверная ошибка (500):
            # raw ForeignKeyViolation иначе долетел бы до write_error → 500.
            if is_foreign_key_violation(exc):
                logger.info(
                    "Cannot delete sensor_type %s: referenced by sensors", type_id_int
                )
                self.write_conflict(
                    "Cannot delete sensor type: sensors with this type exist"
                )
                return
            raise

        if not success:
            self.write_not_found("Sensor type not found")
            return

        self.set_status(204)
        self.finish()
