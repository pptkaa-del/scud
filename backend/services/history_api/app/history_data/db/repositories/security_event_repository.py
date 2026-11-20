from __future__ import annotations

from datetime import datetime
from enum import Enum, auto
import json
from typing import Any

from dacite import Config, from_dict

from shared.database import AsyncPostgres, SqlParam

from ..models.security_event import EventSeverity, SecurityEvent, SecurityEventType

_DACITE_CONFIG = Config(cast=[EventSeverity, SecurityEventType])


class ResolveOutcome(Enum):
    """Результат попытки закрыть (resolve) событие безопасности (COR-08).

    Различает три исхода идемпотентного UPDATE'а, чтобы handler мог отдать
    корректный HTTP-статус:
    - RESOLVED: событие успешно закрыто этим вызовом (200);
    - ALREADY_RESOLVED: событие уже было закрыто ранее — повтор не должен
      переписывать audit-trail (409);
    - NOT_FOUND: события с таким id не существует (404).
    """

    RESOLVED = auto()
    ALREADY_RESOLVED = auto()
    NOT_FOUND = auto()


class SecurityEventRepository:
    """
    Репозиторий для работы с событиями безопасности.

    Обеспечивает создание, получение и закрытие событий безопасности
    (нарушения пороговых значений, взломы, отключения и т.д.).
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def create(
        self,
        locker_id: str | None,
        event_type: str,
        severity: EventSeverity,
        sensor_id: str | None = None,
        violated_thresholds: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
        event_uid: str | None = None,
    ) -> SecurityEvent | None:
        """
        Создать новое событие безопасности.

        Args:
            locker_id: UUID шкафа (None для событий без привязки к шкафу,
                например ``command_2fa_failed`` при ненайденном challenge_id)
            event_type: Тип события ('threshold_violation', 'intrusion',
                'offline', 'door_tamper', 'command_2fa_*', 'command_late_execution')
            severity: Уровень критичности
            sensor_id: UUID датчика (опционально)
            violated_thresholds: Массив нарушенных пороговых значений
            metadata: Дополнительные метаданные
            event_uid: продьюсер-генерируемый UUID для идемпотентности (ARC-02).
                При повторной доставке того же ``event_uid`` INSERT — no-op
                (``ON CONFLICT DO NOTHING``), метод возвращает ``None``.

        Returns:
            Созданное событие безопасности, либо ``None`` если событие с таким
            ``event_uid`` уже существует (дубликат при at-least-once-доставке).
        """
        query = """
            INSERT INTO security_events (
                locker_id, sensor_id, event_type, severity,
                violated_thresholds, metadata, event_uid
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_uid) DO NOTHING
            RETURNING *
        """

        row = await self._db.fetch_one(
            query,
            (
                locker_id,
                sensor_id,
                event_type,
                severity.value,
                json.dumps(violated_thresholds) if violated_thresholds else '[]',
                json.dumps(metadata) if metadata else '{}',
                event_uid,
            ),
            read_only=False,
        )

        if row is None:
            # ON CONFLICT DO NOTHING вернул 0 строк → дубликат по event_uid.
            return None

        return from_dict(SecurityEvent, row, config=_DACITE_CONFIG)

    async def get_by_filters(
        self,
        locker_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        resolved: bool | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 100,
        offset: int = 0
    ) -> list[SecurityEvent]:
        """
        Получить события безопасности с фильтрацией.

        Args:
            locker_id: UUID шкафа
            event_type: Тип события
            severity: Уровень критичности
            resolved: Статус разрешения (True - закрыто, False - открыто, None - все)
            from_date: Начало временного диапазона
            to_date: Конец временного диапазона
            limit: Максимальное количество записей
            offset: Смещение для пагинации

        Returns:
            Список событий безопасности
        """
        query = "SELECT * FROM security_events WHERE 1=1"
        params: list[SqlParam] = []

        if locker_id:
            query += " AND locker_id = %s"
            params.append(locker_id)

        if event_type:
            query += " AND event_type = %s"
            params.append(event_type)

        if severity:
            query += " AND severity = %s"
            params.append(severity)

        if resolved is not None:
            if resolved:
                query += " AND resolved_at IS NOT NULL"
            else:
                query += " AND resolved_at IS NULL"

        if from_date:
            query += " AND occurred_at >= %s"
            params.append(from_date)

        if to_date:
            query += " AND occurred_at <= %s"
            params.append(to_date)

        query += " ORDER BY occurred_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = await self._db.fetch_all(query, tuple(params))
        return [from_dict(SecurityEvent, row, config=_DACITE_CONFIG) for row in rows]

    async def get_count(
        self,
        locker_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        resolved: bool | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None
    ) -> int:
        """
        Получить количество событий с фильтрацией.

        Args:
            locker_id: UUID шкафа
            event_type: Тип события
            severity: Уровень критичности
            resolved: Статус разрешения
            from_date: Начало временного диапазона
            to_date: Конец временного диапазона

        Returns:
            Количество событий
        """
        query = "SELECT COUNT(*) as count FROM security_events WHERE 1=1"
        params: list[SqlParam] = []

        if locker_id:
            query += " AND locker_id = %s"
            params.append(locker_id)

        if event_type:
            query += " AND event_type = %s"
            params.append(event_type)

        if severity:
            query += " AND severity = %s"
            params.append(severity)

        if resolved is not None:
            if resolved:
                query += " AND resolved_at IS NOT NULL"
            else:
                query += " AND resolved_at IS NULL"

        if from_date:
            query += " AND occurred_at >= %s"
            params.append(from_date)

        if to_date:
            query += " AND occurred_at <= %s"
            params.append(to_date)

        row = await self._db.fetch_one(query, tuple(params) if params else ())
        return row['count'] if row else 0

    async def resolve_event(
        self,
        event_id: int,
        resolved_by: int
    ) -> tuple[ResolveOutcome, SecurityEvent | None]:
        """
        Идемпотентно закрыть (resolve) событие безопасности (COR-08).

        UPDATE защищён условием ``resolved_at IS NULL``: закрытие применяется
        только к ещё-не-закрытым событиям, поэтому повторный вызов не
        переписывает audit-trail (кто/когда закрыл). Если UPDATE затронул 0
        строк, отдельным SELECT различаем «уже закрыто» и «не существует».

        Args:
            event_id: ID события
            resolved_by: ID пользователя, закрывающего событие
                (берётся из аутентифицированного контекста, не из тела запроса)

        Returns:
            Кортеж (исход, событие). Событие не-None только при RESOLVED.
        """
        query = """
            UPDATE security_events
            SET resolved_at = now(),
                resolved_by = %s
            WHERE id = %s AND resolved_at IS NULL
            RETURNING *
        """

        row = await self._db.fetch_one(
            query,
            (resolved_by, event_id),
            read_only=False
        )

        if row:
            event = from_dict(SecurityEvent, row, config=_DACITE_CONFIG)
            return ResolveOutcome.RESOLVED, event

        # 0 строк обновлено: либо событие уже закрыто, либо не существует.
        existing = await self._db.fetch_one(
            "SELECT id FROM security_events WHERE id = %s",
            (event_id,),
        )
        if existing:
            return ResolveOutcome.ALREADY_RESOLVED, None
        return ResolveOutcome.NOT_FOUND, None
