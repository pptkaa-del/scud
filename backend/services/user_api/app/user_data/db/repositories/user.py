from __future__ import annotations

from typing import Any

import bcrypt
from dacite import Config, from_dict

from shared.database import AsyncPostgres, SqlParam, require_found
from shared.search import like_filter

from ..models.user import User, UserRole, UserState

_DACITE_CONFIG = Config(cast=[UserState, UserRole])


class UserRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_by_id(self, user_id: int, include_deleted: bool = False) -> User | None:
        row = await self._db.fetch_one(
            """
            SELECT u.*,
                   c.name AS contractor_name,
                   COALESCE(ua.require_2fa, FALSE) AS require_2fa
            FROM users u
            LEFT JOIN contractors c ON u.contractor_id = c.id
            LEFT JOIN user_auth ua ON ua.user_id = u.id
            WHERE u.id = %s
              AND (u.state <> %s OR %s)
            """,
            (user_id, UserState.DELETED.value, include_deleted),
        )
        if not row:
            return None

        return from_dict(User, row, config=_DACITE_CONFIG)

    async def get_full_names_by_ids(self, ids: list[int]) -> dict[int, str]:
        """Вернуть ФИО для переданных user_id одним запросом.

        Используется internal-эндпоинтом для resolve-а ФИО по списку id
        (например, reports_api для «Отчёта по открытию»). Имена возвращаются
        для ЛЮБОГО существующего пользователя независимо от state —
        исторический отчёт должен показывать, кто открывал шкаф, даже если
        пользователь впоследствии деактивирован/удалён. Отсутствующие id
        просто не попадают в результат.
        """
        if not ids:
            return {}
        rows = await self._db.fetch_all(
            "SELECT id, full_name FROM users WHERE id = ANY(%s)",
            (ids,),
        )
        return {int(row["id"]): row["full_name"] for row in rows}

    async def get_by_phone(self, phone: str) -> User | None:
        row = await self._db.fetch_one(
            """
            SELECT u.*,
                   c.name AS contractor_name,
                   COALESCE(ua.require_2fa, FALSE) AS require_2fa
            FROM users u
            LEFT JOIN contractors c ON u.contractor_id = c.id
            LEFT JOIN user_auth ua ON ua.user_id = u.id
            WHERE u.phone = %s
              AND u.state <> %s
            """,
            (phone, UserState.DELETED.value),
        )
        if not row:
            return None

        return from_dict(User, row, config=_DACITE_CONFIG)

    async def set_max_user_id(self, user_id: int, max_user_id: int) -> bool:
        """Атомарно проставить users.max_user_id (UPSERT semantics).

        Возвращает True если row обновлён; False, если такого user'а нет
        или max_user_id уже занят другим активным аккаунтом (partial
        unique index `ux_users_max_user_id_not_deleted` бросает
        IntegrityError, которую обёртывает QueryError → возвращаем False
        наружу как 409).
        """
        from shared.database.errors import QueryError
        try:
            row = await self._db.fetch_one(
                """
                UPDATE users
                SET max_user_id = %s,
                    updated_at = now()
                WHERE id = %s
                  AND state <> 'deleted'
                RETURNING id
                """,
                (max_user_id, user_id),
                read_only=False,
            )
        except QueryError:
            # Конфликт уникальности max_user_id — кто-то уже привязан.
            return False
        return row is not None

    async def get_max_context(
        self, user_id: int,
    ) -> dict[str, object] | None:
        """Вернуть {require_2fa, max_user_id, phone} для step-up flow в auth_api.

        LEFT JOIN на user_auth: пользователь существует, но может не иметь
        записи user_auth (пароль ещё не выставлен) — в этом случае
        require_2fa=NULL → возвращаем False. ``phone`` (NOT NULL в users)
        нужен auth_api для SMS-primary доставки OTP command-2FA, когда
        max_user_id ещё не привязан.
        """
        row = await self._db.fetch_one(
            """
            SELECT u.id, ua.require_2fa, u.max_user_id, u.phone
            FROM users u
            LEFT JOIN user_auth ua ON ua.user_id = u.id
            WHERE u.id = %s
            """,
            (user_id,),
            read_only=True,
        )
        if row is None:
            return None
        return {
            "require_2fa": row["require_2fa"] if row["require_2fa"] is not None else False,
            "max_user_id": row["max_user_id"],
            "phone": row["phone"],
        }

    async def create(
            self,
            phone: str,
            full_name: str,
            created_by: int | None,
            contractor_id: int | None = None,
            role: UserRole = UserRole.WORKER,
    ) -> User:
        # Сначала создаем пользователя
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO users (phone,
                               full_name,
                               contractor_id,
                               created_by,
                               updated_by,
                               role)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                phone,
                full_name,
                contractor_id,
                created_by,
                created_by,
                role,
            ),
            read_only=False,
        )
        # Затем получаем полный объект с contractor_name через JOIN.
        # require_found сужает Optional → User и логирует invariant violation.
        user_id = row['id']
        return require_found(
            await self.get_by_id(user_id),
            entity="User", id_=user_id,
        )

    async def get_all(
            self,
            state: str | None = None,
            contractor_id: int | None = None,
            role: UserRole | None = None,
            q: str | None = None,
            include_deleted: bool = False,
            limit: int = 100,
            offset: int = 0,
    ) -> list[User]:
        """
        Получить список пользователей с опциональной фильтрацией и пагинацией

        Args:
            state: Фильтр по статусу (active, deactivated, deleted)
            contractor_id: Фильтр по ID подрядчика
            role: Фильтр по роли (worker, operator, administrator)
            q: Поиск по имени (ILIKE, подстрока)
            include_deleted: Если True, в результат включаются soft-удалённые пользователи (по умолчанию исключаются).
            limit: Максимальное количество записей
            offset: Смещение от начала списка

        Returns:
            Список пользователей
        """
        # Базовый запрос
        query = """
            SELECT u.*,
                   c.name AS contractor_name,
                   COALESCE(ua.require_2fa, FALSE) AS require_2fa
            FROM users u
            LEFT JOIN contractors c ON u.contractor_id = c.id
            LEFT JOIN user_auth ua ON ua.user_id = u.id
            WHERE 1=1
        """
        params: list[SqlParam] = []

        # Soft-delete фильтр (default-deny): исключаем удалённых пользователей,
        # пока явно не запрошено обратное.
        query += " AND (u.state <> %s OR %s)"
        params.append(UserState.DELETED.value)
        params.append(include_deleted)

        # Добавляем фильтры динамически
        if state is not None:
            query += " AND u.state = %s"
            params.append(state)

        if contractor_id is not None:
            query += " AND u.contractor_id = %s"
            params.append(contractor_id)

        if role is not None:
            query += " AND u.role = %s"
            params.append(role)

        if q:
            clause, qps = like_filter(q, "u.full_name")
            query += f" AND {clause}"
            params.extend(qps)

        query += " ORDER BY u.created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = await self._db.fetch_all(query, tuple(params))
        return [from_dict(User, row, config=_DACITE_CONFIG) for row in rows]

    async def count(
            self,
            state: str | None = None,
            contractor_id: int | None = None,
            role: UserRole | None = None,
            q: str | None = None,
            include_deleted: bool = False,
    ) -> int:
        """Подсчёт пользователей с теми же фильтрами, что и get_all()."""
        query = "SELECT COUNT(*) as count FROM users u WHERE 1=1"
        params: list[SqlParam] = []

        # Soft-delete фильтр (default-deny): исключаем удалённых пользователей,
        # пока явно не запрошено обратное.
        query += " AND (u.state <> %s OR %s)"
        params.append(UserState.DELETED.value)
        params.append(include_deleted)

        if state is not None:
            query += " AND u.state = %s"
            params.append(state)

        if contractor_id is not None:
            query += " AND u.contractor_id = %s"
            params.append(contractor_id)

        if role is not None:
            query += " AND u.role = %s"
            params.append(role)

        if q:
            clause, qps = like_filter(q, "u.full_name")
            query += f" AND {clause}"
            params.extend(qps)

        row = await self._db.fetch_one(query, tuple(params) if params else None)
        return row["count"] if row else 0

    async def update(
            self,
            user_id: int,
            updated_by: int,
            phone: str | None = None,
            full_name: str | None = None,
            role: UserRole | None = None,
            contractor_id: int | None = None,
    ) -> User | None:
        """
        Частичное обновление пользователя.

        Обновляются только переданные поля (не None).
        Возвращает обновлённого пользователя или None если не найден.
        """
        fields: list[str] = []
        params: list[SqlParam] = []

        if phone is not None:
            fields.append("phone = %s")
            params.append(phone)
        if full_name is not None:
            fields.append("full_name = %s")
            params.append(full_name)
        if role is not None:
            fields.append("role = %s")
            params.append(role)
        if contractor_id is not None:
            fields.append("contractor_id = %s")
            params.append(contractor_id)

        if not fields:
            return await self.get_by_id(user_id)

        fields.append("updated_at = now()")
        fields.append("updated_by = %s")
        params.append(updated_by)
        params.append(user_id)

        await self._db.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE id = %s",
            tuple(params),
        )
        return await self.get_by_id(user_id)

    async def deactivate(self, user_id: int, updated_by: int) -> None:
        await self._db.execute(
            """
            UPDATE users
            SET state      = %s,
                updated_at = now(),
                updated_by = %s
            WHERE id = %s
            """,
            (UserState.DEACTIVATED.value, updated_by, user_id),
        )

    async def reactivate(self, user_id: int, updated_by: int) -> User | None:
        """Перевести пользователя в ACTIVE одним UPDATE с RETURNING.

        Если строка не существовала или уже ACTIVE — UPDATE ничего не
        вернёт, и мы делаем fallback get_by_id (идемпотентный ответ).
        """
        # UPDATE с predicate `state <> ACTIVE`: row=None означает либо
        # отсутствие user_id, либо уже ACTIVE. Обе ветки идемпотентно
        # отдают текущее состояние через get_by_id.
        await self._db.fetch_one(
            """
            UPDATE users
            SET state      = %s,
                updated_at = now(),
                updated_by = %s
            WHERE id = %s AND state <> %s
            RETURNING id
            """,
            (UserState.ACTIVE.value, updated_by, user_id, UserState.ACTIVE.value),
            read_only=False,
        )
        return await self.get_by_id(user_id)

    async def get_password_hash(self, user_id: int) -> str | None:
        """Вернуть password_hash из user_auth для данного пользователя."""
        row = await self._db.fetch_one(
            "SELECT password_hash FROM user_auth WHERE user_id = %s",
            (user_id,),
        )
        return row["password_hash"] if row else None

    async def get_auth_record(self, user_id: int) -> dict[str, Any] | None:
        """Вернуть password_hash и require_2fa из user_auth."""
        return await self._db.fetch_one(
            "SELECT password_hash, require_2fa FROM user_auth WHERE user_id = %s",
            (user_id,),
        )

    async def refresh_expire_date(self, user_id: int) -> None:
        """Пересчитать expire_date пользователя как MAX(end_at) среди активных допусков."""
        await self._db.execute(
            """
            UPDATE users
            SET expire_date = (
                SELECT MAX(end_at)
                FROM user_accesses
                WHERE user_id = %s AND is_active = true
            )
            WHERE id = %s
            """,
            (user_id, user_id),
        )

    async def set_password_hash(self, user_id: int, password_hash: str,
                                require_2fa: bool = True) -> None:
        """Создать/обновить user_auth, задавая И пароль, И флаг require_2fa.

        Используется при создании пользователя (2FA включён по умолчанию) и
        при сидинге администратора (явно require_2fa=False). Для смены ТОЛЬКО
        пароля без сброса 2FA используйте update_password.
        """
        hashed = bcrypt.hashpw(password_hash.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        await self._db.execute(
            """
            INSERT INTO user_auth (user_id, password_hash, require_2fa)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE
                SET password_hash = EXCLUDED.password_hash,
                    require_2fa   = EXCLUDED.require_2fa
            """,
            (user_id, hashed, require_2fa),
        )

    async def update_password(self, user_id: int, password_hash: str) -> None:
        """Сменить ТОЛЬКО пароль, сохранив текущий require_2fa.

        При обновлении существующей записи флаг require_2fa не трогается
        (смена пароля не должна включать/выключать 2FA). Если записи ещё нет
        (пароль не был задан) — вставляем, require_2fa берётся из DEFAULT
        колонки (TRUE).
        """
        hashed = bcrypt.hashpw(password_hash.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        await self._db.execute(
            """
            INSERT INTO user_auth (user_id, password_hash)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE
                SET password_hash = EXCLUDED.password_hash
            """,
            (user_id, hashed),
        )

    async def set_require_2fa(self, user_id: int, require_2fa: bool) -> None:
        """Обновить ТОЛЬКО флаг require_2fa в user_auth, не трогая password_hash."""
        await self._db.execute(
            "UPDATE user_auth SET require_2fa = %s WHERE user_id = %s",
            (require_2fa, user_id),
        )
