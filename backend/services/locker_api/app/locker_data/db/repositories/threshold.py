from __future__ import annotations

from typing import Any

from dacite import from_dict

from shared.database import AsyncPostgres

from ....sensor_states import int_like
from ..models.sensor_type import SensorValueType
from ..models.threshold import SensorThreshold

_BOOLEAN_TRUE_TOKENS = ("true", "1")
_BOOLEAN_FALSE_TOKENS = ("false", "0")

_CANONICAL_TRUE = "true"
_CANONICAL_FALSE = "false"


def _fixed_value_key_for_type(raw: Any, value_type: str) -> str:
    """Привести сохранённый fixed_value к ключу states; ненормализуемое остаётся как есть."""
    if value_type == SensorValueType.BOOLEAN:
        try:
            return normalize_boolean_fixed_value(raw)
        except ValueError:
            pass
    elif (key := int_like(raw)) is not None:
        return str(key)
    return str(raw).strip().lower()


def normalize_boolean_fixed_value(value: Any) -> str:
    """Канонизировать ``fixed_value`` boolean-датчика в ``"true"/"false"``.

    Принимает Python ``bool`` и строковые формы ``true/false`` (любой
    регистр) и числовые ``1/0``. Возвращает каноническую форму, которую
    производит :func:`_normalize_for_compare` (lower-case ``"true"/"false"``),
    чтобы сравнение порога с нормализованным значением датчика совпадало.

    Бросает :class:`ValueError` для значений, которые нельзя однозначно
    трактовать как boolean (например ``"banana"`` или ``"2"``). Валидация
    выполняется на write-boundary (создание/обновление порога), чтобы в БД
    не попадал порог, который никогда не совпадёт с ``"true"/"false"`` и
    потому даёт вечное ложное нарушение.
    """
    if isinstance(value, bool):
        return _CANONICAL_TRUE if value else _CANONICAL_FALSE
    token = str(value).strip().lower()
    if token in _BOOLEAN_TRUE_TOKENS:
        return _CANONICAL_TRUE
    if token in _BOOLEAN_FALSE_TOKENS:
        return _CANONICAL_FALSE
    raise ValueError(
        f"fixed_value {value!r} не является булевым значением "
        f"(ожидается true/false)"
    )


def _normalize_for_compare(value: Any, fixed_value: str | None = None) -> str:
    """Нормализовать значение для сравнения с ``fixed_value``.

    Канон:
    - Python ``bool`` → ``'true'/'false'`` (lower case).
    - Остальные типы → ``str(value)``.

    Защитный слой: если ``fixed_value`` явно ``"true"`` или ``"false"`` (то
    есть порог трактует датчик как boolean), числовые ``0/0.0/1/1.0`` тоже
    приводим к ``"false"/"true"``. Это страхует от остаточных некастенных
    значений в БД (когда телеметрия ещё не успела перезаписать старое
    ``{"v": 0.0}`` для bool-датчика). На write-boundary в
    ``device_handler._handle_telemetry`` мы коэрсим в канонический тип, так
    что после первого тика этот защитный слой обычно не нужен — но он
    дёшев и устойчив к будущим багам сериализации.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if fixed_value is not None and fixed_value.lower() in ("true", "false"):
        if isinstance(value, int | float):
            return "true" if value else "false"
        if isinstance(value, str):
            return value.strip().lower()
    return str(value)


def _normalize_stored_fixed_value(fixed_value: str) -> str:
    """Нормализовать хранимый ``fixed_value`` для сравнения (belt-and-suspenders).

    Для boolean-порогов (``true/false`` в любом регистре) приводим к канону
    ``"true"/"false"``. Для нечисловых/строковых порогов (``"open"``, ``"50.0"``)
    возвращаем значение как есть — их сравнение остаётся точным.
    """
    if fixed_value.strip().lower() in (_CANONICAL_TRUE, _CANONICAL_FALSE):
        return fixed_value.strip().lower()
    return fixed_value


def _is_numeric(value: Any) -> bool:
    """True для int/float, но НЕ для bool (isinstance(True, int) → True)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


class ThresholdRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_by_id(self, threshold_id: int) -> SensorThreshold | None:
        """Получить активный порог по ID. Soft-deleted (is_active=false) — не возвращается."""
        row = await self._db.fetch_one(
            """
            SELECT *
            FROM sensor_thresholds
            WHERE id = %s AND is_active = true
            """,
            (threshold_id,),
        )
        if not row:
            return None

        return from_dict(SensorThreshold, row)

    async def get_by_sensor(
        self,
        sensor_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SensorThreshold]:
        """Получить активные пороговые значения для датчика с пагинацией."""
        rows = await self._db.fetch_all(
            """
            SELECT *
            FROM sensor_thresholds
            WHERE sensor_id = %s AND is_active = true
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (sensor_id, limit, offset),
        )
        return [from_dict(SensorThreshold, row) for row in rows]

    async def count_by_sensor(self, sensor_id: str) -> int:
        """Общее количество активных порогов датчика."""
        row = await self._db.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM sensor_thresholds
            WHERE sensor_id = %s AND is_active = true
            """,
            (sensor_id,),
        )
        return int(row["count"]) if row else 0

    async def count_active_range_by_type(self, type_id: int) -> int:
        row = await self._db.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM sensor_thresholds t
            JOIN sensors s ON s.id = t.sensor_id
            WHERE s.sensor_type_id = %s AND s.is_active = true AND t.is_active = true
              AND (t.min_value IS NOT NULL OR t.max_value IS NOT NULL)
            """,
            (type_id,),
        )
        return int(row["count"]) if row else 0

    async def active_fixed_values_by_type(self, type_id: int, value_type: str) -> set[str]:
        rows = await self._db.fetch_all(
            """
            SELECT DISTINCT t.fixed_value
            FROM sensor_thresholds t
            JOIN sensors s ON s.id = t.sensor_id
            WHERE s.sensor_type_id = %s AND s.is_active = true AND t.is_active = true
              AND t.fixed_value IS NOT NULL
            """,
            (type_id,),
        )
        return {_fixed_value_key_for_type(r["fixed_value"], value_type) for r in rows}

    async def create(
            self,
            sensor_id: str,
            created_by: int | None,
            min_value: float | None = None,
            max_value: float | None = None,
            fixed_value: str | None = None,
            description: str | None = None,
    ) -> SensorThreshold:
        """Создать новое пороговое значение"""
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO sensor_thresholds (sensor_id,
                                           min_value,
                                           max_value,
                                           fixed_value,
                                           description,
                                           created_by,
                                           updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (sensor_id, min_value, max_value, fixed_value, description, created_by, created_by),
        )
        return from_dict(SensorThreshold, row)

    async def update(
            self,
            threshold_id: int,
            updated_by: int,
            min_value: float | None = None,
            max_value: float | None = None,
            fixed_value: str | None = None,
            description: str | None = None,
    ) -> SensorThreshold | None:
        """Обновить пороговое значение"""
        row = await self._db.fetch_one(
            """
            UPDATE sensor_thresholds
            SET min_value = COALESCE(%s, min_value),
                max_value = COALESCE(%s, max_value),
                fixed_value = COALESCE(%s, fixed_value),
                description = COALESCE(%s, description),
                updated_at = now(),
                updated_by = %s
            WHERE id = %s
            RETURNING *
            """,
            (min_value, max_value, fixed_value, description, updated_by, threshold_id),
            read_only=False,
        )
        if not row:
            return None

        return from_dict(SensorThreshold, row)

    async def set_active_status(
            self,
            threshold_id: int,
            is_active: bool,
            updated_by: int,
    ) -> SensorThreshold | None:
        """Активировать/деактивировать пороговое значение"""
        row = await self._db.fetch_one(
            """
            UPDATE sensor_thresholds
            SET is_active = %s,
                updated_at = now(),
                updated_by = %s
            WHERE id = %s
            RETURNING *
            """,
            (is_active, updated_by, threshold_id),
            read_only=False,
        )
        if not row:
            return None

        return from_dict(SensorThreshold, row)

    async def delete(self, threshold_id: int) -> bool:
        """Soft-delete порога — пометить is_active=false.

        Запись остаётся в БД, чтобы исторические security_events с violated_thresholds
        ссылались на интерпретируемые пороги.
        """
        row = await self._db.fetch_one(
            """
            UPDATE sensor_thresholds
            SET is_active = false,
                updated_at = now()
            WHERE id = %s AND is_active = true
            RETURNING id
            """,
            (threshold_id,),
            read_only=False,
        )
        return row is not None

    @staticmethod
    def evaluate_thresholds(
            value: Any,
            thresholds: list[Any],
    ) -> tuple[bool, list[str]]:
        """Чистая (без БД) проверка ``value`` против списка порогов.

        Вынесено отдельным методом, чтобы StateMonitor мог одним SQL-запросом
        достать (sensor, threshold)-пары и вызывать проверку in-memory
        вместо N+1.

        ``thresholds`` — duck-typed: каждый элемент должен иметь атрибуты
        ``min_value``, ``max_value``, ``fixed_value`` (как ``SensorThreshold``).
        """
        if not thresholds:
            return True, []

        violations = []

        for threshold in thresholds:
            min_value = threshold.min_value
            max_value = threshold.max_value
            fixed_value = threshold.fixed_value

            # min/max применимы только к numeric (исключаем bool — он subclass int)
            if _is_numeric(value):
                if min_value is not None and value < min_value:
                    violations.append(
                        f"Значение {value} меньше минимального {min_value}"
                    )

                if max_value is not None and value > max_value:
                    violations.append(
                        f"Значение {value} больше максимального {max_value}"
                    )

            # fixed_value проверяется для любого типа через нормализацию.
            # Защитный слой: устаревшие записи могли сохранить boolean-порог в
            # неканоническом регистре ("True"/"FALSE") до валидации на
            # write-boundary — нормализуем и хранимую сторону, чтобы
            # совпадающее значение не давало вечного ложного нарушения.
            if (
                fixed_value is not None
                and value is not None
                and _normalize_for_compare(value, fixed_value)
                != _normalize_stored_fixed_value(fixed_value)
            ):
                violations.append(
                    f"Значение {value} не соответствует ожидаемому {fixed_value}"
                )

        return len(violations) == 0, violations
