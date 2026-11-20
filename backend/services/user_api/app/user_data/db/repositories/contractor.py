from __future__ import annotations

from dacite import from_dict

from shared.database import AsyncPostgres, SqlParam
from shared.search import like_filter

from ..models.contractor import Contractor

_CONTRACTOR_STATE_ACTIVE = "active"
_CONTRACTOR_STATE_ARCHIVED = "archived"


class ContractorRepository:
    """Репозиторий подрядчиков.

    Soft-delete: `delete()` переводит подрядчика в state='archived'.
    Сохраняем запись, чтобы пользователи с этим contractor_id оставались
    интерпретируемыми (FK ON DELETE SET NULL не теряет связь автоматически).
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_by_id(self, contractor_id: int) -> Contractor | None:
        row = await self._db.fetch_one(
            """
            SELECT *
            FROM contractors
            WHERE id = %s AND state = %s
            """,
            (contractor_id, _CONTRACTOR_STATE_ACTIVE),
        )
        if not row:
            return None

        return from_dict(Contractor, row)

    async def get_all(
        self,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Contractor]:
        query = "SELECT * FROM contractors WHERE state = %s"
        params: list[SqlParam] = [_CONTRACTOR_STATE_ACTIVE]

        if q:
            clause, qps = like_filter(q, "name")
            query += f" AND {clause}"
            params.extend(qps)

        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = await self._db.fetch_all(query, tuple(params))
        return [from_dict(Contractor, row) for row in rows]

    async def count(self, q: str | None = None) -> int:
        query = "SELECT COUNT(*) AS count FROM contractors WHERE state = %s"
        params: list[SqlParam] = [_CONTRACTOR_STATE_ACTIVE]

        if q:
            clause, qps = like_filter(q, "name")
            query += f" AND {clause}"
            params.extend(qps)

        row = await self._db.fetch_one(query, tuple(params))
        return int(row["count"]) if row else 0

    async def create(
            self,
            name: str,
            created_by: int | None,
    ) -> Contractor:
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO contractors (name, created_by)
            VALUES (%s, %s)
            RETURNING *
            """,
            (name, created_by),
        )
        return from_dict(Contractor, row)

    async def update(
            self,
            contractor_id: int,
            name: str,
    ) -> Contractor | None:
        row = await self._db.fetch_one(
            """
            UPDATE contractors
            SET name = %s
            WHERE id = %s AND state = %s
            RETURNING *
            """,
            (name, contractor_id, _CONTRACTOR_STATE_ACTIVE),
            read_only=False,
        )
        if not row:
            return None

        return from_dict(Contractor, row)

    async def delete(self, contractor_id: int) -> bool:
        """Архивировать подрядчика (soft-delete).

        Возвращает True, если запись была активной и переведена в archived.
        Повторный delete для уже архивированного — False.
        """
        row = await self._db.fetch_one(
            """
            UPDATE contractors
            SET state = %s
            WHERE id = %s AND state = %s
            RETURNING id
            """,
            (_CONTRACTOR_STATE_ARCHIVED, contractor_id, _CONTRACTOR_STATE_ACTIVE),
            read_only=False,
        )
        return row is not None
