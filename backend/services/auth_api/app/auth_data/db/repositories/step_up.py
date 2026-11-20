"""Репозиторий step_up_challenges.

Чтение (``select``) и последующие записи выполняются отдельными запросами
через ``shared.database.AsyncPostgres``; каждый запрос открывает свою
короткую транзакцию и коммитит её на выходе из контекстного менеджера. Это
значит, что row-lock между ``select`` и записью **не удерживается** —
никакой ``SELECT ... FOR UPDATE`` тут не дал бы того, что обычно от него
ожидают. Поэтому результат ``select`` неавторитетен и пригоден лишь для
быстрых проверок/диагностики.

Корректность гарантируют исключительно два метода, и каждый — это
единственный атомарный CAS-``UPDATE`` с инвариантом прямо в ``WHERE``:

- ``reserve_attempt`` — атомарно расходует бюджет попыток
  (``attempts < max_attempts AND used_at IS NULL AND expires_at > now()``);
  при N параллельных попытках каждая списывает бюджет ровно один раз, гонка
  check→compare→increment не обходит ``max_attempts``;
- ``consume`` — атомарно «сжигает» challenge как single-use
  (``used_at IS NULL AND expires_at > now()``); при гонке параллельных
  ``verify`` ровно один вызов получит строку в ``RETURNING``, остальные —
  ноль строк.

Дополнительно handler в ``locker_api`` ещё раз проверяет соответствие
``scope_payload.locker_id`` URL-параметру, поэтому подмена scope между
``verify`` и фактическим выполнением команды невозможна.
"""
from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from shared.database import AsyncPostgres


class StepUpChallengeRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def insert(
        self,
        *,
        challenge_id: str,
        user_id: int,
        scope_kind: str,
        scope_payload: dict[str, Any],
        code_hash: str,
        expires_at: datetime,
        max_attempts: int = 5,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO step_up_challenges
                (id, user_id, scope_kind, scope_payload, code_hash,
                 max_attempts, expires_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
            """,
            (challenge_id, user_id, scope_kind, json.dumps(scope_payload),
             code_hash, max_attempts, expires_at),
        )

    async def select(self, challenge_id: str) -> dict[str, Any] | None:
        """Прочитать challenge-строку по id.

        Результат неавторитетен: авторитетные решения принимают атомарные
        ``reserve_attempt`` (бюджет попыток) и ``consume`` (single-use) в
        последующих вызовах — каждый CAS-``UPDATE`` держит инвариант в
        ``WHERE``. Поля ``used_at``/``expires_at`` из этого ``select``
        годятся лишь для быстрых fast-path проверок и диагностики. Сверка
        ``scope_payload.locker_id`` с URL ``locker_id`` в ``locker_api``
        ловит попытки переиспользовать scope.
        """
        row = await self._db.fetch_one(
            """
            SELECT id::text AS id, user_id, scope_kind, scope_payload,
                   code_hash, attempts, max_attempts, expires_at, used_at,
                   created_at
            FROM step_up_challenges
            WHERE id = %s
            """,
            (challenge_id,),
            read_only=True,
        )
        if row is None:
            return None
        return dict(row)

    async def reserve_attempt(self, challenge_id: str) -> dict[str, Any] | None:
        """Атомарно зарезервировать попытку перед сравнением кода (SEC-13).

        Один CAS-``UPDATE`` инкрементирует ``attempts`` ровно тогда, когда
        challenge ещё активен и бюджет не исчерпан. При N параллельных
        попытках каждая расходует бюджет ровно один раз — гонка
        check→compare→increment больше не позволяет обойти ``max_attempts``.

        Returns:
            ``{"attempts", "code_hash"}`` при успешном резерве (вызывающий
            затем сравнивает код с ``code_hash``); ``None`` — бюджет исчерпан,
            challenge использован или истёк (вызывающий ОБЯЗАН отклонить).
        """
        row = await self._db.fetch_one(
            """
            UPDATE step_up_challenges
            SET attempts = attempts + 1
            WHERE id = %s
              AND attempts < max_attempts
              AND used_at IS NULL
              AND expires_at > now()
            RETURNING attempts, code_hash
            """,
            (challenge_id,),
            read_only=False,
        )
        if row is None:
            return None
        return dict(row)

    async def consume(self, challenge_id: str) -> bool:
        """Атомарно «сжечь» challenge как single-use (SEC-12).

        Один CAS-``UPDATE`` помечает ``used_at`` ровно для ещё не
        использованного и не просроченного challenge. При двух параллельных
        ``verify`` Postgres сериализует апдейты: первый получит строку в
        ``RETURNING``, второй увидит ``used_at IS NOT NULL`` и вернёт 0 строк.

        Returns:
            True — challenge успешно потреблён (вызывающий вправе считать
            verify успешным). False — challenge уже использован или истёк;
            вызывающий ОБЯЗАН отклонить запрос.
        """
        row = await self._db.fetch_one(
            """
            UPDATE step_up_challenges
            SET used_at = now()
            WHERE id = %s AND used_at IS NULL AND expires_at > now()
            RETURNING id
            """,
            (challenge_id,),
            read_only=False,
        )
        return row is not None

    async def delete_expired(self) -> None:
        await self._db.execute(
            "DELETE FROM step_up_challenges WHERE expires_at < now()",
        )
