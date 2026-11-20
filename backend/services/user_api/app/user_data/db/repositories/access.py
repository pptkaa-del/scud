"""Репозиторий для управления допусками пользователей к шкафам"""
from __future__ import annotations

from dacite import from_dict

from shared.database import AsyncPostgres, SqlParam, require_found

from ....sentinel import UNSET, Unset
from ..models.access import UserAccessWithLockers


class AccessRepository:
    """
    Репозиторий для управления допусками пользователей

    Обеспечивает CRUD операции с user_accesses и user_access_lockers.
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_by_id(self, access_id: int) -> UserAccessWithLockers | None:
        """
        Получение допуска по ID с информацией о шкафах

        Args:
            access_id: ID допуска

        Returns:
            Допуск с деталями или None если не найден
        """
        # Получаем основную информацию о допуске
        row = await self._db.fetch_one(
            """
            SELECT
                ua.id,
                ua.user_id,
                ua.start_at,
                ua.end_at,
                ua.granted_by,
                u_granter.full_name AS granted_by_full_name,
                ua.request_id,
                ua.full_access,
                ua.is_active,
                ua.created_at
            FROM user_accesses ua
            LEFT JOIN users u_granter ON ua.granted_by = u_granter.id
            WHERE ua.id = %s
            """,
            (access_id,),
        )

        if not row:
            return None

        # Получаем список шкафов, если не full_access
        lockers = []
        if not row["full_access"]:
            locker_rows = await self._db.fetch_all(
                """
                SELECT locker_id
                FROM user_access_lockers
                WHERE user_access_id = %s
                ORDER BY locker_id
                """,
                (access_id,),
            )
            lockers = [r["locker_id"] for r in locker_rows]

        # Собираем объект
        access_dict = dict(row)
        access_dict["lockers"] = lockers

        return from_dict(UserAccessWithLockers, access_dict)

    async def has_active_grant(self, user_id: int, locker_id: str) -> bool:
        """Есть ли у пользователя активный Допуск на конкретный шкаф.

        Семантика «активного» Допуска совпадает с ACM
        (``acm/app/db.py::get_active_access``):
          - is_active = true
          - start_at <= NOW()
          - end_at IS NULL OR end_at > NOW()

        ``full_access`` Допуск покрывает любой шкаф. Сравнение по
        ``locker_id`` идёт через каст ``%s::uuid`` — Postgres нормализует
        любую форму UUID (dashed/dashless/braced/uppercase) к одному виду,
        поэтому отдельная канонизация на стороне Python не нужна.

        Args:
            user_id:   ID пользователя.
            locker_id: UUID шкафа (любая валидная форма).

        Returns:
            True если найден хотя бы один активный Допуск, покрывающий шкаф.
        """
        row = await self._db.fetch_one(
            """
            SELECT 1 AS granted
            FROM user_accesses ua
            LEFT JOIN user_access_lockers ual ON ual.user_access_id = ua.id
            WHERE ua.user_id = %s
              AND ua.is_active = true
              AND ua.start_at <= now()
              AND (ua.end_at IS NULL OR ua.end_at > now())
              AND (ua.full_access = true OR ual.locker_id = %s::uuid)
            LIMIT 1
            """,
            (user_id, locker_id),
        )
        return row is not None

    async def get_all_for_user(
        self,
        user_id: int,
        include_inactive: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UserAccessWithLockers]:
        """
        Получение допусков пользователя с пагинацией.

        Args:
            user_id: ID пользователя
            include_inactive: Включать ли деактивированные допуски
            limit: Максимальное число записей
            offset: Смещение

        Returns:
            Список допусков пользователя (текущая страница)
        """
        query = """
            SELECT
                ua.id,
                ua.user_id,
                ua.start_at,
                ua.end_at,
                ua.granted_by,
                u_granter.full_name AS granted_by_full_name,
                ua.request_id,
                ua.full_access,
                ua.is_active,
                ua.created_at
            FROM user_accesses ua
            LEFT JOIN users u_granter ON ua.granted_by = u_granter.id
            WHERE ua.user_id = %s
        """

        if not include_inactive:
            query += " AND ua.is_active = true"

        query += " ORDER BY ua.created_at DESC LIMIT %s OFFSET %s"

        rows = await self._db.fetch_all(query, (user_id, limit, offset))

        accesses = []
        for row in rows:
            # Получаем список шкафов для каждого допуска
            lockers = []
            if not row["full_access"]:
                locker_rows = await self._db.fetch_all(
                    """
                    SELECT locker_id
                    FROM user_access_lockers
                    WHERE user_access_id = %s
                    ORDER BY locker_id
                    """,
                    (row["id"],),
                )
                lockers = [r["locker_id"] for r in locker_rows]

            access_dict = dict(row)
            access_dict["lockers"] = lockers
            accesses.append(from_dict(UserAccessWithLockers, access_dict))

        return accesses

    async def count_for_user(
        self,
        user_id: int,
        include_inactive: bool = False,
    ) -> int:
        """Общее количество допусков пользователя (для пагинации)."""
        query = "SELECT COUNT(*) AS count FROM user_accesses WHERE user_id = %s"
        if not include_inactive:
            query += " AND is_active = true"
        row = await self._db.fetch_one(query, (user_id,))
        return int(row["count"]) if row else 0

    async def create(
        self,
        user_id: int,
        start_at: str,  # datetime as ISO string
        granted_by: int,
        end_at: str | None = None,
        request_id: int | None = None,
        full_access: bool = False,
        locker_ids: list[str] | None = None,
    ) -> UserAccessWithLockers:
        """
        Создание нового допуска для пользователя

        Args:
            user_id: ID пользователя
            start_at: Дата начала действия допуска (ISO format)
            granted_by: ID пользователя, предоставившего допуск
            end_at: Дата окончания действия допуска (опционально)
            request_id: ID заявки на доступ (опционально)
            full_access: Если True, доступ ко всем шкафам
            locker_ids: Список ID шкафов (игнорируется если full_access=True)

        Returns:
            Созданный допуск с деталями
        """
        # Создаем запись в user_accesses
        access_row = await self._db.fetch_one_strict(
            """
            INSERT INTO user_accesses (
                user_id, start_at, end_at, granted_by, request_id, full_access, is_active
            )
            VALUES (%s, %s, %s, %s, %s, %s, true)
            RETURNING *
            """,
            (user_id, start_at, end_at, granted_by, request_id, full_access),
            read_only=False,
        )

        access_id = access_row["id"]

        # Если не full_access и указаны шкафы, добавляем записи в user_access_lockers
        if not full_access and locker_ids:
            for locker_id in locker_ids:
                await self._db.execute(
                    """
                    INSERT INTO user_access_lockers (user_access_id, locker_id)
                    VALUES (%s, %s)
                    """,
                    (access_id, locker_id),
                )

        # Возвращаем полный объект — require_found сужает Optional → T.
        return require_found(
            await self.get_by_id(access_id),
            entity="UserAccess", id_=access_id,
        )

    async def update(
        self,
        access_id: int,
        start_at: str | None = None,
        end_at: str | None | Unset = UNSET,
        locker_ids: list[str] | None = None,
    ) -> UserAccessWithLockers | None:
        """
        Обновление допуска (продление срока или изменение списка шкафов)

        Args:
            access_id: ID допуска
            start_at: Новая дата начала (опционально)
            end_at: Новая дата окончания (опционально)
            locker_ids: Новый список ID шкафов (только для не-full_access)

        Returns:
            Обновленный допуск или None если не найден
        """
        # Проверяем существование
        existing = await self.get_by_id(access_id)
        if not existing:
            return None

        # Обновляем даты если указаны
        if start_at is not None or end_at is not UNSET:
            update_query = "UPDATE user_accesses SET "
            params: list[SqlParam] = []

            if start_at is not None:
                update_query += "start_at = %s, "
                params.append(start_at)

            if not isinstance(end_at, Unset):
                update_query += "end_at = %s, "
                params.append(end_at)  # None → SQL NULL; str → datetime value

            update_query = update_query.rstrip(", ")
            update_query += " WHERE id = %s"
            params.append(access_id)

            await self._db.execute(update_query, tuple(params))

        # Обновляем список шкафов если указаны и не full_access
        if locker_ids is not None and not existing.full_access:
            # Удаляем старые записи
            await self._db.execute(
                "DELETE FROM user_access_lockers WHERE user_access_id = %s",
                (access_id,),
            )

            # Добавляем новые
            for locker_id in locker_ids:
                await self._db.execute(
                    """
                    INSERT INTO user_access_lockers (user_access_id, locker_id)
                    VALUES (%s, %s)
                    """,
                    (access_id, locker_id),
                )

        # 404-путь уже отдан через early return `if not existing: return None`;
        # на этой ветке строка точно есть (UPDATE не удаляет).
        return require_found(
            await self.get_by_id(access_id),
            entity="UserAccess", id_=access_id,
        )

    async def deactivate(self, access_id: int) -> UserAccessWithLockers | None:
        """
        Деактивация допуска (установка is_active=false)

        Допуск не удаляется физически, а помечается как неактивный.

        Args:
            access_id: ID допуска

        Returns:
            Деактивированный допуск или None если не найден
        """
        # Проверяем существование
        existing = await self.get_by_id(access_id)
        if not existing:
            return None

        if not existing.is_active:
            # Уже деактивирован
            return existing

        # Деактивируем
        await self._db.execute(
            """
            UPDATE user_accesses
            SET is_active = false
            WHERE id = %s
            """,
            (access_id,),
        )

        return require_found(
            await self.get_by_id(access_id),
            entity="UserAccess", id_=access_id,
        )

    async def reactivate(self, access_id: int) -> UserAccessWithLockers | None:
        """
        Реактивация допуска (установка is_active=true)

        Args:
            access_id: ID допуска

        Returns:
            Реактивированный допуск или None если не найден
        """
        # Проверяем существование
        existing = await self.get_by_id(access_id)
        if not existing:
            return None

        if existing.is_active:
            # Уже активен
            return existing

        # Реактивируем
        await self._db.execute(
            """
            UPDATE user_accesses
            SET is_active = true
            WHERE id = %s
            """,
            (access_id,),
        )

        return require_found(
            await self.get_by_id(access_id),
            entity="UserAccess", id_=access_id,
        )

    async def get_all_for_users(
        self,
        user_ids: list[int],
        include_inactive: bool = False,
    ) -> dict[int, list[UserAccessWithLockers]]:
        """
        Батч-получение допусков для нескольких пользователей.

        Выполняет два запроса вместо N+1:
        1. Все допуски для всех запрошенных user_ids.
        2. Все связанные шкафы для найденных допусков.

        Args:
            user_ids: Список ID пользователей
            include_inactive: Включать ли деактивированные допуски

        Returns:
            Словарь {user_id: [допуски]}. Пользователи без допусков
            присутствуют в результате с пустым списком.
        """
        result: dict[int, list[UserAccessWithLockers]] = {uid: [] for uid in user_ids}

        if not user_ids:
            return result

        query = """
            SELECT
                ua.id,
                ua.user_id,
                ua.start_at,
                ua.end_at,
                ua.granted_by,
                u_granter.full_name AS granted_by_full_name,
                ua.request_id,
                ua.full_access,
                ua.is_active,
                ua.created_at
            FROM user_accesses ua
            LEFT JOIN users u_granter ON ua.granted_by = u_granter.id
            WHERE ua.user_id = ANY(%s)
        """

        if not include_inactive:
            query += " AND ua.is_active = true"

        query += " ORDER BY ua.user_id, ua.created_at DESC"

        rows = await self._db.fetch_all(query, (user_ids,))

        if not rows:
            return result

        # Собираем шкафы для всех допусков одним запросом
        access_ids = [row["id"] for row in rows if not row["full_access"]]
        lockers_map: dict[int, list[str]] = {}
        if access_ids:
            locker_rows = await self._db.fetch_all(
                """
                SELECT user_access_id, locker_id
                FROM user_access_lockers
                WHERE user_access_id = ANY(%s)
                ORDER BY locker_id
                """,
                (access_ids,),
            )
            for lr in locker_rows:
                lockers_map.setdefault(lr["user_access_id"], []).append(lr["locker_id"])

        for row in rows:
            access_dict = dict(row)
            access_dict["lockers"] = lockers_map.get(row["id"], []) if not row["full_access"] else []
            access = from_dict(UserAccessWithLockers, access_dict)
            result[row["user_id"]].append(access)

        return result

    async def get_expired_accesses(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UserAccessWithLockers]:
        """
        Получение списка истекших допусков

        Args:
            limit: Максимальное количество записей
            offset: Смещение для пагинации

        Returns:
            Список истекших допусков
        """
        rows = await self._db.fetch_all(
            """
            SELECT
                ua.id,
                ua.user_id,
                ua.start_at,
                ua.end_at,
                ua.granted_by,
                u_granter.full_name AS granted_by_full_name,
                ua.request_id,
                ua.full_access,
                ua.is_active,
                ua.created_at
            FROM user_accesses ua
            LEFT JOIN users u_granter ON ua.granted_by = u_granter.id
            WHERE ua.is_active = true
              AND ua.end_at IS NOT NULL
              AND ua.end_at < now()
            ORDER BY ua.end_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )

        accesses = []
        for row in rows:
            # Получаем список шкафов для каждого допуска
            lockers = []
            if not row["full_access"]:
                locker_rows = await self._db.fetch_all(
                    """
                    SELECT locker_id
                    FROM user_access_lockers
                    WHERE user_access_id = %s
                    ORDER BY locker_id
                    """,
                    (row["id"],),
                )
                lockers = [r["locker_id"] for r in locker_rows]

            access_dict = dict(row)
            access_dict["lockers"] = lockers
            accesses.append(from_dict(UserAccessWithLockers, access_dict))

        return accesses
