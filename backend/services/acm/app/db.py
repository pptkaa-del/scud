"""
Репозиторий для read-only чтения данных пользователя из БД user_api.

ACM подключается к той же БД (scud_users), что и user_api, но использует
только чтение. Запрашивает:
  - роль и состояние пользователя (таблица users)
  - активные Допуски (user_accesses + user_access_lockers)

Использует dacite для типобезопасного маппинга строк БД на dataclass-модели.
"""
from __future__ import annotations

from dataclasses import dataclass

from dacite import Config, from_dict

from shared.database import AsyncPostgres

# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UserRecord:
    """Запись пользователя из таблиц users + user_auth (LEFT JOIN)."""
    id: int
    role: str    # "worker" | "operator" | "administrator"
    state: str   # "active" | "deactivated" | "deleted"
    require_2fa: bool  # из user_auth.require_2fa; FALSE если строки нет


@dataclass(frozen=True, slots=True)
class AccessRecord:
    """Агрегированные данные Допуска: является ли полным и список шкафов."""
    full_access: bool
    locker_ids: list[str]  # пустой список если full_access=True


_USER_DACITE_CONFIG = Config(cast=[])


# ---------------------------------------------------------------------------
# Репозиторий
# ---------------------------------------------------------------------------

class UserRepository:
    """Read-only доступ к данным пользователя для ACM."""

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_by_id(self, user_id: int) -> UserRecord | None:
        """
        Возвращает роль, состояние пользователя и флаг require_2fa.
        None если пользователь не найден.
        """
        row = await self._db.fetch_one(
            """
            SELECT u.id, u.role, u.state,
                   COALESCE(ua.require_2fa, FALSE) AS require_2fa
            FROM users u
            LEFT JOIN user_auth ua ON ua.user_id = u.id
            WHERE u.id = %s
            """,
            (user_id,),
            read_only=True,
        )
        if row is None:
            return None
        return from_dict(UserRecord, dict(row), config=_USER_DACITE_CONFIG)

    async def get_active_access(self, user_id: int) -> AccessRecord:
        """
        Возвращает агрегированные данные активных Допусков пользователя.

        Допуск активен если:
          - is_active = true
          - start_at <= NOW()
          - end_at IS NULL OR end_at > NOW()

        Если хотя бы один Допуск имеет full_access=true — возвращает full_access=True
        с пустым списком шкафов (доступ ко всем).
        """
        rows = await self._db.fetch_all(
            """
            SELECT ua.full_access, ual.locker_id
            FROM user_accesses ua
            LEFT JOIN user_access_lockers ual ON ual.user_access_id = ua.id
            WHERE ua.user_id = %s
              AND ua.is_active = true
              AND ua.start_at <= NOW()
              AND (ua.end_at IS NULL OR ua.end_at > NOW())
            """,
            (user_id,),
        )

        if not rows:
            return AccessRecord(full_access=False, locker_ids=[])

        for row in rows:
            if row["full_access"]:
                return AccessRecord(full_access=True, locker_ids=[])

        locker_ids: list[str] = [
            str(row["locker_id"])
            for row in rows
            if row["locker_id"] is not None
        ]
        return AccessRecord(full_access=False, locker_ids=locker_ids)
