"""
Репозиторий для работы с типами датчиков (sensor_types).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Final

from dacite import Config, from_dict

from shared.database import AsyncPostgres

from ....sensor_states import states_from_db, states_to_json
from ..models.sensor_type import SensorStateOption, SensorType, SensorValueType

_DACITE_CONFIG = Config(cast=[SensorValueType])

class StatesUpdate(Enum):
    KEEP = "keep"


KEEP_STATES: Final = StatesUpdate.KEEP
_COLUMNS: Final = "id, code, name, description, unit, value_type, created_at, states"


def _to_sensor_type(row: dict[str, Any]) -> SensorType:
    raw_states = row.get("states")
    sensor_type = from_dict(SensorType, {**row, "states": None}, config=_DACITE_CONFIG)
    sensor_type.states = states_from_db(raw_states, sensor_type.value_type)
    return sensor_type


def _states_param(states: list[SensorStateOption] | None) -> str | None:
    return None if states is None else states_to_json(states)


class SensorTypeRepository:
    """Репозиторий для CRUD операций с типами датчиков"""

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_by_id(self, type_id: int) -> SensorType | None:
        """
        Получить тип датчика по ID.

        Args:
            type_id: ID типа датчика

        Returns:
            Объект SensorType или None, если не найден
        """
        row = await self._db.fetch_one(
            f"""
            SELECT {_COLUMNS}
            FROM sensor_types
            WHERE id = %s
            """,
            (type_id,),
        )
        if not row:
            return None

        return _to_sensor_type(row)

    async def get_by_code(self, code: str) -> SensorType | None:
        """
        Получить тип датчика по коду.

        Args:
            code: Код типа датчика (temperature, humidity, etc.)

        Returns:
            Объект SensorType или None, если не найден
        """
        row = await self._db.fetch_one(
            f"""
            SELECT {_COLUMNS}
            FROM sensor_types
            WHERE code = %s
            """,
            (code,),
        )
        if not row:
            return None

        return _to_sensor_type(row)

    async def get_all(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SensorType]:
        """
        Получить страницу типов датчиков.

        Returns:
            Список объектов SensorType (текущая страница).
        """
        rows = await self._db.fetch_all(
            f"""
            SELECT {_COLUMNS}
            FROM sensor_types
            ORDER BY code
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        return [_to_sensor_type(row) for row in rows]

    async def count(self) -> int:
        """Общее количество типов датчиков."""
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS count FROM sensor_types",
        )
        return int(row["count"]) if row else 0

    async def create(
        self,
        code: str,
        name: str,
        value_type: str,
        description: str | None = None,
        unit: str | None = None,
        states: list[SensorStateOption] | None = None,
    ) -> SensorType:
        """
        Создать новый тип датчика.

        Args:
            code: Уникальный код типа
            name: Название типа
            value_type: Тип значения (numeric, boolean, string)
            description: Описание типа (опционально)
            unit: Единица измерения (опционально)
            states: Подписи перечислимых значений (опционально)

        Returns:
            Созданный объект SensorType
        """
        row = await self._db.fetch_one_strict(
            f"""
            INSERT INTO sensor_types (code, name, description, unit, value_type, states)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            RETURNING {_COLUMNS}
            """,
            (code, name, description, unit, value_type, _states_param(states)),
        )
        return _to_sensor_type(row)

    async def update(
        self,
        type_id: int,
        name: str | None = None,
        description: str | None = None,
        unit: str | None = None,
        value_type: str | None = None,
        states: list[SensorStateOption] | None | StatesUpdate = KEEP_STATES,
    ) -> SensorType | None:
        """
        Обновить тип датчика.

        Args:
            type_id: ID типа датчика
            name: Новое название (опционально)
            description: Новое описание (опционально)
            unit: Новая единица измерения (опционально)
            value_type: Новый тип значения (опционально)
            states: Новые подписи; None очищает, KEEP_STATES не меняет

        Returns:
            Обновленный объект SensorType или None, если не найден
        """
        # Получаем текущие значения
        current = await self.get_by_id(type_id)
        if not current:
            return None

        # Используем новые значения или оставляем текущие
        new_name = name if name is not None else current.name
        new_description = description if description is not None else current.description
        new_unit = unit if unit is not None else current.unit
        new_value_type = value_type if value_type is not None else current.value_type

        assignments = "name = %s, description = %s, unit = %s, value_type = %s"
        params: list[object] = [new_name, new_description, new_unit, new_value_type]
        if not isinstance(states, StatesUpdate):
            assignments += ", states = %s::jsonb"
            params.append(_states_param(states))
        params.append(type_id)

        row = await self._db.fetch_one(
            f"""
            UPDATE sensor_types
            SET {assignments}
            WHERE id = %s
            RETURNING {_COLUMNS}
            """,
            tuple(params),
            read_only=False,
        )
        if not row:
            return None

        return _to_sensor_type(row)

    async def delete(self, type_id: int) -> bool:
        """
        Удалить тип датчика.

        Note: Удаление возможно только если нет датчиков с этим типом (FK constraint)

        Args:
            type_id: ID типа датчика

        Returns:
            True если удален, False если не найден
        """
        row = await self._db.fetch_one(
            """
            DELETE FROM sensor_types
            WHERE id = %s
            RETURNING id
            """,
            (type_id,),
            read_only=False,
        )
        return row is not None
