from __future__ import annotations

import json
from typing import Any

from dacite import from_dict

from shared.database import AsyncPostgres, require_found

from ....sensor_states import resolve_display, states_from_db
from ..models.sensor import Sensor


def _pack_value(value: Any) -> str | None:
    """Упаковать Python-значение в JSONB-обёртку {"v": value}.

    None → None (NULL в БД); остальные типы (bool, int, float, str) — в JSON.
    Передаётся в SQL с явным `::jsonb` cast.
    """
    if value is None:
        return None
    return json.dumps({"v": value})


def _unpack_value(raw: Any) -> Any:
    """Достать типизированное значение из JSONB-обёртки.

    psycopg2 возвращает JSONB как Python dict; на всякий случай поддерживаем
    и сырую json-строку (если адаптер jsonb не зарегистрирован).
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw.get("v")
    if isinstance(raw, str):
        try:
            return json.loads(raw).get("v")
        except (ValueError, AttributeError):
            return None
    return raw


def _row_to_sensor(row: dict[str, Any]) -> Sensor:
    """Распаковать current_value и вычислить display из states типа датчика."""
    raw_states = row.get("states")
    row = {k: v for k, v in row.items() if k != "states"}
    row["current_value"] = _unpack_value(row.get("current_value"))
    sensor = from_dict(Sensor, row)
    sensor.display = resolve_display(
        states_from_db(raw_states, sensor.value_type), sensor.current_value,
    )
    return sensor


class SensorRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_type_info(self, sensor_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Получить ``sensor_id → {"code": ..., "value_type": ...}`` одним запросом.

        Возвращает и ``code`` (sensor_type_code для записи в history), и
        ``value_type`` (для проверки типа значения) одним SELECT, чтобы не
        плодить roundtrip'ы.

        Не фильтрует по ``is_active`` — телеметрия от soft-deleted датчиков
        в принципе не должна приходить, но если придёт — type_info всё равно
        нужен, чтобы корректно записать в history.
        """
        if not sensor_ids:
            return {}
        rows = await self._db.fetch_all(
            """
            SELECT s.id, st.code, st.value_type
            FROM sensors s
            JOIN sensor_types st ON s.sensor_type_id = st.id
            WHERE s.id = ANY(%s::uuid[])
            """,
            ([str(sid) for sid in sensor_ids],),
        )
        return {
            str(r["id"]): {"code": r["code"], "value_type": r["value_type"]}
            for r in rows
        }

    async def get_id_by_locker_and_type_code(
        self, locker_id: str, sensor_type_code: str
    ) -> str | None:
        """Вернуть UUID активного датчика шкафа по коду типа (или None).

        Используется релейным MQTT-ингестом: телеметрия платы (battery/signal)
        приходит без явного sensor_id, его надо разрезолвить по
        (locker_id, sensor_types.code). Берём самый ранний активный датчик
        нужного типа (ORDER BY created_at) — детерминированно при нескольких.
        """
        row = await self._db.fetch_one(
            """
            SELECT s.id
            FROM sensors s
            JOIN sensor_types st ON s.sensor_type_id = st.id
            WHERE s.locker_id = %s
              AND st.code = %s
              AND s.is_active = true
            ORDER BY s.created_at
            LIMIT 1
            """,
            (locker_id, sensor_type_code),
        )
        return str(row["id"]) if row else None

    async def get_or_create_id_by_type_code(
        self, locker_id: str, sensor_type_code: str, serial_number: str
    ) -> str | None:
        """Вернуть UUID датчика шкафа по коду типа, создав его при отсутствии.

        Идемпотентно и устойчиво к гонке параллельных stat'ов: INSERT идёт с
        ON CONFLICT DO NOTHING по партиальному уникальному индексу
        ux_sensors_serial_locker_active (locker_id, serial_number WHERE
        is_active), после чего датчик всё равно перечитывается. Если код типа
        не найден в sensor_types (нет seed) — возвращает None, ничего не создав.

        Релейный MQTT-ингест зовёт это лениво на первый stat: WS-локеры получают
        sensors через API, а relay-локеры — здесь, чтобы у телеметрии был
        реальный UUID sensor_id (history_api.sensor_telemetry.sensor_id — UUID).
        """
        existing = await self.get_id_by_locker_and_type_code(locker_id, sensor_type_code)
        if existing is not None:
            return existing

        type_row = await self._db.fetch_one(
            "SELECT id FROM sensor_types WHERE code = %s",
            (sensor_type_code,),
        )
        if not type_row:
            return None

        await self._db.execute(
            """
            INSERT INTO sensors (locker_id, serial_number, sensor_type_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (locker_id, serial_number) WHERE is_active DO NOTHING
            """,
            (locker_id, serial_number, type_row["id"]),
        )
        # Перечитываем: либо нашу строку, либо вставленную параллельным stat'ом.
        return await self.get_id_by_locker_and_type_code(locker_id, sensor_type_code)

    async def get_by_id(self, sensor_id: str) -> Sensor | None:
        """
        Получить активный датчик по ID.

        Soft-delete: датчик с is_active=false (удалённый) недоступен через API.
        Используйте `update_active_status(sensor_id, True)` для восстановления.
        """
        row = await self._db.fetch_one(
            """
            SELECT s.id, s.locker_id, s.serial_number, s.sensor_type_id,
                   s.current_value, s.created_at, s.updated_at, s.is_active, s.metadata,
                   st.code, st.name, st.unit, st.value_type, st.states
            FROM sensors s
            JOIN sensor_types st ON s.sensor_type_id = st.id
            WHERE s.id = %s AND s.is_active = true
            """,
            (sensor_id,),
        )
        if not row:
            return None

        return _row_to_sensor(row)

    async def get_by_locker(
        self,
        locker_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Sensor]:
        """Получить активные датчики конкретного шкафа с пагинацией.

        Soft-deleted (is_active=false) датчики не возвращаются.
        """
        rows = await self._db.fetch_all(
            """
            SELECT s.id, s.locker_id, s.serial_number, s.sensor_type_id,
                   s.current_value, s.created_at, s.updated_at, s.is_active, s.metadata,
                   st.code, st.name, st.unit, st.value_type, st.states
            FROM sensors s
            JOIN sensor_types st ON s.sensor_type_id = st.id
            WHERE s.locker_id = %s
              AND s.is_active = true
            ORDER BY s.sensor_type_id, s.serial_number
            LIMIT %s OFFSET %s
            """,
            (locker_id, limit, offset),
        )
        return [_row_to_sensor(row) for row in rows]

    async def count_by_locker(self, locker_id: str) -> int:
        """Общее количество активных датчиков шкафа."""
        row = await self._db.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM sensors
            WHERE locker_id = %s AND is_active = true
            """,
            (locker_id,),
        )
        return int(row["count"]) if row else 0

    async def create(
            self,
            locker_id: str,
            serial_number: str,
            sensor_type_id: int,
            current_value: Any = None,
    ) -> Sensor:
        """
        Создать новый датчик.

        Args:
            locker_id: UUID шкафа
            serial_number: Серийный номер датчика
            sensor_type_id: ID типа датчика
            current_value: Начальное значение (bool | float | str | None);
                           тип должен соответствовать sensor_types.value_type.

        Returns:
            Созданный объект Sensor
        """
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO sensors (locker_id,
                                 serial_number,
                                 sensor_type_id,
                                 current_value)
            VALUES (%s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (locker_id, serial_number, sensor_type_id, _pack_value(current_value)),
            read_only=False,
        )
        # Затем получаем полный объект с полями из sensor_types через JOIN.
        sensor_id = row['id']
        return require_found(
            await self.get_by_id(sensor_id),
            entity="Sensor", id_=sensor_id,
        )

    async def update_value(
            self,
            sensor_id: str,
            value: Any,
    ) -> None:
        """Обновить текущее значение датчика (любого типа: bool/numeric/string).

        Hot-path: вызывается на каждый sensor в каждом телеметрическом
        батче, поэтому возвращает None — обновлённый объект нужно
        запрашивать через `get_by_id` отдельно при необходимости.
        """
        await self._db.execute(
            """
            UPDATE sensors
            SET current_value = %s::jsonb,
                updated_at = now()
            WHERE id = %s
            """,
            (_pack_value(value), sensor_id),
        )

    async def swap_value(self, sensor_id: str, value: Any) -> tuple[bool, Any]:
        """Записать значение и вернуть (изменилось ли, прежнее значение).

        Прежнее и новое сравниваются в одной команде под блокировкой строки,
        поэтому переход видит ровно один из конкурентных вызовов. Прежнее None —
        значение ещё ни разу не записывалось.
        """
        packed = _pack_value(value)
        row = await self._db.fetch_one(
            """
            WITH prev AS (
                SELECT id, current_value
                FROM sensors
                WHERE id = %(sensor_id)s
                FOR UPDATE
            )
            UPDATE sensors AS s
            SET current_value = %(value)s::jsonb,
                updated_at = now()
            FROM prev
            WHERE s.id = prev.id
              AND prev.current_value IS DISTINCT FROM %(value)s::jsonb
            RETURNING prev.current_value AS previous
            """,
            {"sensor_id": sensor_id, "value": packed},
            read_only=False,
        )
        if row is None:
            return False, None
        return True, _unpack_value(row["previous"])

    async def update_values(
            self,
            updates: list[tuple[str, Any]],
    ) -> None:
        """Batch-обновление current_value для нескольких датчиков одним UPDATE.

        Батч-версия `update_value` (C-3): телеметрический батч на N датчиков
        раньше делал N отдельных UPDATE round-trip'ов в цикле. Здесь —
        один statement: `unnest` разворачивает параллельные id/val массивы
        во временный набор строк, дальше единственный JOIN-UPDATE по id.
        Сериализация значения идентична `update_value` (`_pack_value` —
        та же JSONB-обёртка `{"v": value}`), поведение записи не меняется.

        Пустой список — no-op без обращения к БД.
        """
        if not updates:
            return
        ids = [str(sensor_id) for sensor_id, _ in updates]
        values = [_pack_value(value) for _, value in updates]
        await self._db.execute(
            """
            UPDATE sensors
            SET current_value = v.val::jsonb,
                updated_at = now()
            FROM unnest(%s::uuid[], %s::jsonb[]) AS v(id, val)
            WHERE sensors.id = v.id
            """,
            (ids, values),
        )

    async def update_active_status(
            self,
            sensor_id: str,
            is_active: bool,
    ) -> Sensor | None:
        """Изменить статус активности датчика"""
        await self._db.execute(
            """
            UPDATE sensors
            SET is_active = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (is_active, sensor_id),
        )
        # Получаем обновленный объект с полями из sensor_types через JOIN
        return await self.get_by_id(sensor_id)

    async def delete(self, sensor_id: str) -> bool:
        """Soft-delete датчика — пометить как is_active=false.

        Запись не удаляется физически: исторические события и пороги остаются
        интерпретируемыми. Восстановление возможно через `update_active_status`.
        Возвращает True, если датчик был активен и был помечен удалённым.
        """
        row = await self._db.fetch_one(
            """
            UPDATE sensors
            SET is_active = false,
                updated_at = now()
            WHERE id = %s AND is_active = true
            RETURNING id
            """,
            (sensor_id,),
            read_only=False,
        )
        return row is not None
