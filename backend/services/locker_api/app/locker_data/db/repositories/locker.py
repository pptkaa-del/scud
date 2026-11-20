from __future__ import annotations

from typing import Any

from dacite import Config, from_dict

from shared.database import AsyncPostgres, SqlParam
from shared.search import like_filter

from ..models.locker import (
    Connectivity,
    DoorState,
    Locker,
    LockerBrief,
    LockerLocation,
    LockerTransport,
    OperationalStatus,
)

_DACITE_CONFIG = Config(cast=[Connectivity, DoorState, OperationalStatus, LockerTransport])
_LOCKER_STATE_ACTIVE = "active"
_LOCKER_STATE_ARCHIVED = "archived"

# Фильтр области видимости по Допуску. ::uuid[] — id колонка uuid, а параметры
# приходят строками (тот же идиом, что в get_batch*).
_SCOPE_CLAUSE = "id = ANY(%s::uuid[])"


class LockerRepository:
    """Репозиторий шкафов.

    Soft-delete: `delete()` переводит шкаф в state='archived'. Все списочные
    и одиночные чтения фильтруют `state='active'`, поэтому архивные шкафы
    не показываются в API. Запись остаётся в БД для целостности истории
    (телеметрия, command_history по locker_id).
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_by_id(self, locker_id: str) -> Locker | None:
        row = await self._db.fetch_one(
            """
            SELECT *
            FROM lockers
            WHERE id = %s AND state = %s
            """,
            (locker_id, _LOCKER_STATE_ACTIVE),
        )
        if not row:
            return None

        return from_dict(Locker, row, config=_DACITE_CONFIG)

    async def get_by_inventory_number(self, inventory_number: str) -> Locker | None:
        row = await self._db.fetch_one(
            """
            SELECT *
            FROM lockers
            WHERE inventory_number = %s AND state = %s
            """,
            (inventory_number, _LOCKER_STATE_ACTIVE),
        )
        if not row:
            return None

        return from_dict(Locker, row, config=_DACITE_CONFIG)

    async def create(
            self,
            inventory_number: str,
            created_by: int | None,
            location_address: str | None = None,
            location_lat: float | None = None,
            location_lon: float | None = None,
            description: str | None = None,
            transport: str = "mqtt",
            relay_id: int | None = None,
    ) -> Locker:
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO lockers (inventory_number,
                                 location_address,
                                 location_lat,
                                 location_lon,
                                 description,
                                 created_by,
                                 updated_by,
                                 transport,
                                 relay_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                inventory_number,
                location_address,
                location_lat,
                location_lon,
                description,
                created_by,
                created_by,
                transport,
                relay_id,
            ),
        )
        return from_dict(Locker, row, config=_DACITE_CONFIG)

    async def get_by_relay_id(self, relay_id: int) -> Locker | None:
        """Найти активный шкаф по relay_id (идентификатор релейной платы)."""
        row = await self._db.fetch_one(
            """
            SELECT *
            FROM lockers
            WHERE relay_id = %s AND state = %s
            """,
            (relay_id, _LOCKER_STATE_ACTIVE),
        )
        if not row:
            return None

        return from_dict(Locker, row, config=_DACITE_CONFIG)

    async def get_locker_id_by_relay_id(self, relay_id: int) -> str | None:
        """Лёгкий lookup id шкафа по relay_id (горячий путь ingest, без гидрации строки)."""
        row = await self._db.fetch_one(
            """
            SELECT id
            FROM lockers
            WHERE relay_id = %s AND state = %s
            """,
            (relay_id, _LOCKER_STATE_ACTIVE),
        )
        return str(row["id"]) if row else None

    async def get_all(
            self,
            connectivity: str | None = None,
            door: str | None = None,
            operational_status: str | None = None,
            limit: int = 100,
            offset: int = 0,
            view: str | None = None,
            q: str | None = None,
            allowed_locker_ids: list[str] | None = None,
    ) -> list[Locker | LockerLocation]:
        """Получить список шкафов с опциональной фильтрацией.

        view='location' возвращает list[LockerLocation] (только id + координаты),
        иначе — list[Locker] с полным набором полей.

        q — текстовый фильтр (case-insensitive ILIKE) по inventory_number
        ИЛИ location_address. handler должен передать уже trimmed
        непустую строку (или None). Экранирование LIKE-метасимволов
        выполняется внутри.

        allowed_locker_ids — область видимости по Допуску (см.
        ``shared.access_control.LockerScope.sql_filter_ids``): None — фильтра
        нет (operator/administrator/full_access), [] — ноль строк. Значение
        обязано совпадать с переданным в ``count``, иначе total разъедется с
        items (как и q).
        """
        if view == "location":
            select_clause = (
                "SELECT id, location_lat, location_lon, location_address, operational_status"
            )
            def row_factory(row: dict[str, Any]) -> Locker | LockerLocation:
                return from_dict(LockerLocation, row, config=_DACITE_CONFIG)
        else:
            select_clause = "SELECT *"
            def row_factory(row: dict[str, Any]) -> Locker | LockerLocation:
                return from_dict(Locker, row, config=_DACITE_CONFIG)

        clauses: list[str] = []
        params: list[SqlParam] = [_LOCKER_STATE_ACTIVE]

        if connectivity is not None:
            clauses.append(" AND connectivity = %s")
            params.append(connectivity)

        if door is not None:
            clauses.append(" AND door = %s")
            params.append(door)

        if operational_status is not None:
            clauses.append(" AND operational_status = %s")
            params.append(operational_status)

        if q:
            clause, qps = like_filter(q, "inventory_number", "location_address")
            clauses.append(f" AND {clause}")
            params.extend(qps)

        if allowed_locker_ids is not None:
            clauses.append(f" AND {_SCOPE_CLAUSE}")
            params.append(list(allowed_locker_ids))

        query = (
            f"{select_clause}\n"
            "            FROM lockers\n"
            "            WHERE state = %s"
            f"{''.join(clauses)}"
            " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])

        rows = await self._db.fetch_all(query, tuple(params))
        return [row_factory(row) for row in rows]

    async def count(
            self,
            connectivity: str | None = None,
            door: str | None = None,
            operational_status: str | None = None,
            q: str | None = None,
            allowed_locker_ids: list[str] | None = None,
    ) -> int:
        """Получить общее количество шкафов с учётом фильтров.

        q и allowed_locker_ids — симметричные с get_all фильтры; оба
        обязательны для согласованности total и items (иначе пагинация обещает
        страницы, которых вызывающий не увидит).
        """
        clauses: list[str] = []
        params: list[SqlParam] = [_LOCKER_STATE_ACTIVE]

        if connectivity is not None:
            clauses.append(" AND connectivity = %s")
            params.append(connectivity)

        if door is not None:
            clauses.append(" AND door = %s")
            params.append(door)

        if operational_status is not None:
            clauses.append(" AND operational_status = %s")
            params.append(operational_status)

        if q:
            clause, qps = like_filter(q, "inventory_number", "location_address")
            clauses.append(f" AND {clause}")
            params.extend(qps)

        if allowed_locker_ids is not None:
            clauses.append(f" AND {_SCOPE_CLAUSE}")
            params.append(list(allowed_locker_ids))

        query = (
            "SELECT COUNT(*) as count\n"
            "            FROM lockers\n"
            "            WHERE state = %s"
            f"{''.join(clauses)}"
        )

        row = await self._db.fetch_one(query, tuple(params))
        return row["count"] if row else 0

    async def update_connectivity(
            self,
            locker_id: str,
            connectivity: Connectivity,
    ) -> Locker | None:
        """Обновить связность шкафа (управляется WS-соединением)"""
        # При переходе в online обновляем last_seen_at
        if connectivity == Connectivity.ONLINE:
            sql = """
                UPDATE lockers
                SET connectivity = %s,
                    last_seen_at = now(),
                    updated_at = now()
                WHERE id = %s
                RETURNING *
            """
        else:
            sql = """
                UPDATE lockers
                SET connectivity = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING *
            """
        row = await self._db.fetch_one(
            sql,
            (connectivity, locker_id),
            read_only=False,
        )
        if not row:
            return None
        return from_dict(Locker, row, config=_DACITE_CONFIG)

    async def mark_offline_if_online(self, locker_id: str) -> bool:
        """Атомарно перевести шкаф online→offline (COR-19).

        Один UPDATE … WHERE connectivity='online' RETURNING: предикат
        перепроверяется в момент записи, поэтому TOCTOU-safe (зеркалит
        offline_monitor.sql_mark_stale_offline). Возвращает True только если
        ИМЕННО этот вызов выполнил переход — два interleaved stat=0 (retained +
        live) для одного шкафа: переход выигрывает ровно один, второй матчит
        0 строк → False и не плодит дубликат offline-события / лишний unregister.
        state='active' исключает архивные шкафы.
        """
        row = await self._db.fetch_one(
            """
            UPDATE lockers
            SET connectivity = 'offline',
                updated_at = now()
            WHERE id = %s AND connectivity = 'online' AND state = 'active'
            RETURNING id
            """,
            (locker_id,),
            read_only=False,
        )
        return row is not None

    async def get_connectivity(self, locker_id: str) -> Connectivity | None:
        """Лёгкая проекция: только connectivity активного шкафа (или None).

        Релейный rep-путь (ingest._is_offline) дёргает это на каждый rep, чтобы
        решить, нужен ли re-query статуса. Полный get_by_id (SELECT * + dacite-
        гидрация всей модели) ради одного поля — лишняя работа на hot path.
        """
        row = await self._db.fetch_one(
            """
            SELECT connectivity
            FROM lockers
            WHERE id = %s AND state = %s
            """,
            (locker_id, _LOCKER_STATE_ACTIVE),
        )
        if not row:
            return None
        return Connectivity(row["connectivity"])

    async def get_operational_status(self, locker_id: str) -> OperationalStatus | None:
        """Лёгкая проекция operational_status (как get_connectivity).

        Нужна горячему MQTT-пути для подавления телеметрии в MAINTENANCE без
        SELECT * + dacite-гидрации всей модели на каждый heartbeat-stat."""
        row = await self._db.fetch_one(
            """
            SELECT operational_status
            FROM lockers
            WHERE id = %s AND state = %s
            """,
            (locker_id, _LOCKER_STATE_ACTIVE),
        )
        if row is None:
            return None
        return OperationalStatus(row["operational_status"])

    async def update_door(
            self,
            locker_id: str,
            door: DoorState,
            who_open_last: int | None = None,
    ) -> Locker | None:
        """Обновить состояние двери (управляется результатами команд).

        При door=OPEN дополнительно проставляет last_opened_at = now() —
        денормализованный кэш времени последнего открытия для быстрых
        запросов карточки шкафа без обращения в history_api. Также
        записывает who_open_last (ID инициатора OPEN-команды) — always-overwrite
        семантика: значение перезаписывается переданным параметром даже
        если он None (system-issued OPEN). Оба поля пишутся одним UPDATE,
        поэтому last_opened_at и who_open_last всегда согласованы.

        При door=CLOSED who_open_last не трогаем (информация «кто последним
        открывал» сохраняется до следующего OPEN).

        state='active' исключает архивные шкафы: поздний ACK устройства по
        команде, выданной до архивации, не мутирует архивную строку (COR-17).
        """
        if door == DoorState.OPEN:
            sql = """
                UPDATE lockers
                SET door = %s,
                    last_opened_at = now(),
                    who_open_last = %s,
                    updated_at = now()
                WHERE id = %s AND state = %s
                RETURNING *
            """
            params: tuple[Any, ...] = (door, who_open_last, locker_id, _LOCKER_STATE_ACTIVE)
        else:
            sql = """
                UPDATE lockers
                SET door = %s,
                    updated_at = now()
                WHERE id = %s AND state = %s
                RETURNING *
            """
            params = (door, locker_id, _LOCKER_STATE_ACTIVE)
        row = await self._db.fetch_one(sql, params, read_only=False)
        if not row:
            return None
        return from_dict(Locker, row, config=_DACITE_CONFIG)

    async def sync_door_state(self, locker_id: str, door: DoorState) -> Locker | None:
        """Синхронизировать физическое состояние двери из ТЕЛЕМЕТРИИ (relay stat).

        В отличие от ``update_door`` (командный путь, авторитетное событие
        открытия), обновляет ТОЛЬКО ``door`` и ``updated_at`` и НИКОГДА не трогает
        ``last_opened_at`` / ``who_open_last``. Владельцем этих полей остаётся
        командный путь (``CommandResultApplier`` → ``update_door``).

        Зачем: relay-плата шлёт heartbeat-stat каждые ~5 минут, повторяя своё
        ЗАЛИПШЕЕ состояние (например door=open, пока дверь открыта). Раньше
        ``ingest._handle_stat`` гонял на каждый такой stat командный
        ``update_door(OPEN)``, который выставлял ``last_opened_at = now()`` и
        затирал ``who_open_last``. В результате «время последнего открытия»
        сбрасывалось каждые 5 минут (UI показывал самопроизвольное
        переоткрытие и не мог посчитать время с момента открытия), а атрибуция
        «кто открыл» терялась. Подтверждено live на стенде.

        Edge-triggered (``AND door <> %s``): апдейт срабатывает ТОЛЬКО при
        реальной смене физического состояния, поэтому повторный heartbeat с тем
        же состоянием — 0 строк (не бампит даже ``updated_at``).

        ``state = 'active'`` исключает архивные шкафы (COR-17, паритет с
        ``update_door``). Возвращает обновлённый ``Locker`` или ``None`` (нет
        смены состояния / шкаф не active / не найден).
        """
        row = await self._db.fetch_one(
            """
            UPDATE lockers
            SET door = %s,
                updated_at = now()
            WHERE id = %s AND state = %s AND door <> %s
            RETURNING *
            """,
            (door, locker_id, _LOCKER_STATE_ACTIVE, door),
            read_only=False,
        )
        if not row:
            return None
        return from_dict(Locker, row, config=_DACITE_CONFIG)

    async def update_operational_status(
            self,
            locker_id: str,
            status: OperationalStatus,
            updated_by: int | None = None,
    ) -> Locker | None:
        """Обновить операционный статус (управляется оператором / StateMonitor)"""
        row = await self._db.fetch_one(
            """
            UPDATE lockers
            SET operational_status = %s,
                updated_at = now(),
                updated_by = %s
            WHERE id = %s AND state = %s
            RETURNING *
            """,
            (status, updated_by, locker_id, _LOCKER_STATE_ACTIVE),
            read_only=False,
        )
        if not row:
            return None
        return from_dict(Locker, row, config=_DACITE_CONFIG)

    async def raise_alarm(self, locker_id: str) -> Locker | None:
        """Перевести шкаф из NORMAL в ALARM; None — уже в тревоге, на обслуживании
        или не active. Одна команда, поэтому переход видит ровно один вызов."""
        row = await self._db.fetch_one(
            """
            UPDATE lockers
            SET operational_status = %s,
                updated_at = now()
            WHERE id = %s AND state = %s AND operational_status = %s
            RETURNING *
            """,
            (OperationalStatus.ALARM, locker_id, _LOCKER_STATE_ACTIVE, OperationalStatus.NORMAL),
            read_only=False,
        )
        if not row:
            return None
        return from_dict(Locker, row, config=_DACITE_CONFIG)

    async def update_board_info(
            self, locker_id: str, board_version: int, has_relays: bool,
    ) -> bool:
        """Версия платы и наличие блока реле из stat; True — значения изменились."""
        row = await self._db.fetch_one(
            """
            UPDATE lockers
            SET board_version = %s,
                has_relays = %s,
                updated_at = now()
            WHERE id = %s AND state = %s
              AND (board_version IS DISTINCT FROM %s OR has_relays IS DISTINCT FROM %s)
            RETURNING id
            """,
            (board_version, has_relays, locker_id, _LOCKER_STATE_ACTIVE,
             board_version, has_relays),
            read_only=False,
            idempotent=True,
        )
        return row is not None

    async def update_last_seen(self, locker_id: str) -> None:
        """Обновить время последнего контакта со шкафом"""
        await self._db.execute(
            """
            UPDATE lockers
            SET last_seen_at = now()
            WHERE id = %s
            """,
            (locker_id,),
        )

    async def update(
            self,
            locker_id: str,
            updated_by: int,
            inventory_number: str | None = None,
            location_address: str | None = None,
            location_lat: float | None = None,
            location_lon: float | None = None,
            description: str | None = None,
    ) -> Locker | None:
        """Обновить данные шкафа"""
        updates: list[str] = []
        params: list[SqlParam] = []

        if inventory_number is not None:
            updates.append("inventory_number = %s")
            params.append(inventory_number)

        if location_address is not None:
            updates.append("location_address = %s")
            params.append(location_address)

        if location_lat is not None:
            updates.append("location_lat = %s")
            params.append(location_lat)

        if location_lon is not None:
            updates.append("location_lon = %s")
            params.append(location_lon)

        if description is not None:
            updates.append("description = %s")
            params.append(description)

        if not updates:
            return await self.get_by_id(locker_id)

        updates.append("updated_at = now()")
        updates.append("updated_by = %s")
        params.append(updated_by)
        params.append(locker_id)

        query = f"""
            UPDATE lockers
            SET {', '.join(updates)}
            WHERE id = %s
            RETURNING *
        """

        row = await self._db.fetch_one(query, tuple(params), read_only=False)
        if not row:
            return None

        return from_dict(Locker, row, config=_DACITE_CONFIG)

    async def delete(self, locker_id: str) -> bool:
        """Архивировать шкаф (soft-delete).

        Запись не удаляется физически — переводится в state='archived'.
        Шкафы в архиве недоступны через API, но связанные исторические данные
        (телеметрия, command_history) остаются доступны по locker_id.
        Повторный архив того же шкафа возвращает False (как и для отсутствующего).
        """
        row = await self._db.fetch_one(
            """
            UPDATE lockers
            SET state = %s,
                updated_at = now()
            WHERE id = %s AND state = %s
            RETURNING id
            """,
            (_LOCKER_STATE_ARCHIVED, locker_id, _LOCKER_STATE_ACTIVE),
            read_only=False,
        )
        return row is not None

    async def get_batch(self, locker_ids: list[str]) -> dict[str, Locker | None]:
        result: dict[str, Locker | None] = {lid: None for lid in locker_ids}
        if not locker_ids:
            return result
        rows = await self._db.fetch_all(
            "SELECT * FROM lockers WHERE id = ANY(%s::uuid[]) AND state = %s",
            (locker_ids, _LOCKER_STATE_ACTIVE),
        )
        for row in rows:
            result[row["id"]] = from_dict(Locker, row, config=_DACITE_CONFIG)
        return result

    async def get_batch_brief(self, locker_ids: list[str]) -> dict[str, LockerBrief | None]:
        result: dict[str, LockerBrief | None] = {lid: None for lid in locker_ids}
        if not locker_ids:
            return result
        rows = await self._db.fetch_all(
            "SELECT id, inventory_number FROM lockers "
            "WHERE id = ANY(%s::uuid[]) AND state = %s",
            (locker_ids, _LOCKER_STATE_ACTIVE),
        )
        for row in rows:
            result[row["id"]] = from_dict(LockerBrief, dict(row), config=_DACITE_CONFIG)
        return result

    async def get_batch_location(self, locker_ids: list[str]) -> dict[str, LockerLocation | None]:
        result: dict[str, LockerLocation | None] = {lid: None for lid in locker_ids}
        if not locker_ids:
            return result
        rows = await self._db.fetch_all(
            "SELECT id, location_lat, location_lon, location_address, operational_status "
            "FROM lockers WHERE id = ANY(%s::uuid[]) AND state = %s",
            (locker_ids, _LOCKER_STATE_ACTIVE),
        )
        for row in rows:
            result[row["id"]] = from_dict(LockerLocation, dict(row), config=_DACITE_CONFIG)
        return result
