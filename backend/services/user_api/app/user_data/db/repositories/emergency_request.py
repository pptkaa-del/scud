"""Репозиторий для работы с заявками на экстренное открытие шкафов"""
from __future__ import annotations

import json
from typing import Any

from dacite import Config, from_dict

from shared.database import AsyncPostgres, SqlParam

from ..models.emergency_request import (
    EmergencyRequest,
    EmergencyRequestAction,
    EmergencyRequestLog,
    EmergencyRequestStatus,
    EmergencyRequestWithDetails,
)

# Конфигурация dacite для правильного каста enum
_DACITE_CONFIG = Config(cast=[EmergencyRequestStatus, EmergencyRequestAction])


class EmergencyRequestRepository:
    """
    Репозиторий для управления заявками на экстренное открытие шкафов

    Обеспечивает CRUD операции и ведение аудит-лога всех действий.
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def create(
        self,
        user_id: int,
        locker_id: str,
    ) -> EmergencyRequest:
        """
        Создание новой заявки на экстренное открытие

        Автоматически создает запись в логе с действием 'created'.

        Args:
            user_id: ID пользователя, подающего заявку
            locker_id: ID шкафа

        Returns:
            Созданная заявка
        """
        # Создаем заявку
        request_row = await self._db.fetch_one_strict(
            """
            INSERT INTO emergency_requests (user_id, locker_id, status)
            VALUES (%s, %s, 'pending')
            RETURNING *
            """,
            (user_id, locker_id),
        )

        request = from_dict(EmergencyRequest, request_row, config=_DACITE_CONFIG)

        # Логируем создание
        await self._log_action(
            request_id=request.id,
            action=EmergencyRequestAction.CREATED,
            performed_by=user_id,
            details={"locker_id": locker_id},
        )

        return request

    async def get_by_id(
        self,
        request_id: int,
        log_view: bool = False,
        viewer_id: int | None = None,
    ) -> EmergencyRequestWithDetails | None:
        """
        Получение заявки по ID с деталями пользователей

        Args:
            request_id: ID заявки
            log_view: Если True, логирует просмотр заявки
            viewer_id: ID пользователя, просматривающего заявку (для лога)

        Returns:
            Заявка с деталями или None если не найдена
        """
        row = await self._db.fetch_one(
            """
            SELECT
                er.id,
                er.user_id,
                u_creator.full_name AS user_full_name,
                u_creator.phone AS user_phone,
                er.locker_id,
                er.status,
                er.created_at,
                er.closed_at,
                er.closed_by,
                u_closer.full_name AS closed_by_full_name
            FROM emergency_requests er
            INNER JOIN users u_creator ON er.user_id = u_creator.id
            LEFT JOIN users u_closer ON er.closed_by = u_closer.id
            WHERE er.id = %s
            """,
            (request_id,),
        )

        if not row:
            return None

        request = from_dict(EmergencyRequestWithDetails, row, config=_DACITE_CONFIG)

        # Опционально логируем просмотр
        if log_view and viewer_id:
            await self._log_action(
                request_id=request_id,
                action=EmergencyRequestAction.VIEWED,
                performed_by=viewer_id,
                details=None,
            )

        return request

    async def get_all(
        self,
        status: EmergencyRequestStatus | None = None,
        locker_id: str | None = None,
        user_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EmergencyRequestWithDetails]:
        """
        Получение списка заявок с фильтрацией и пагинацией

        Args:
            status: Фильтр по статусу (pending/closed)
            locker_id: Фильтр по ID шкафа
            user_id: Фильтр по автору заявки (scoping worker'а на свои заявки,
                SEC-09). None — без ограничения (operator/administrator).
            limit: Максимальное количество записей (по умолчанию 100)
            offset: Смещение для пагинации (по умолчанию 0)

        Returns:
            Список заявок с деталями
        """
        # Базовый запрос
        query = """
            SELECT
                er.id,
                er.user_id,
                u_creator.full_name AS user_full_name,
                u_creator.phone AS user_phone,
                er.locker_id,
                er.status,
                er.created_at,
                er.closed_at,
                er.closed_by,
                u_closer.full_name AS closed_by_full_name
            FROM emergency_requests er
            INNER JOIN users u_creator ON er.user_id = u_creator.id
            LEFT JOIN users u_closer ON er.closed_by = u_closer.id
            WHERE 1=1
        """
        params: list[SqlParam] = []

        # Добавляем фильтры динамически
        if status is not None:
            query += " AND er.status = %s"
            params.append(status.value if isinstance(status, EmergencyRequestStatus) else status)

        if locker_id is not None:
            query += " AND er.locker_id = %s"
            params.append(locker_id)

        if user_id is not None:
            query += " AND er.user_id = %s"
            params.append(user_id)

        # Сортировка и пагинация
        query += " ORDER BY er.created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = await self._db.fetch_all(query, tuple(params) if params else None)
        return [from_dict(EmergencyRequestWithDetails, row, config=_DACITE_CONFIG) for row in rows]

    async def close(
        self,
        request_id: int,
        closed_by: int,
    ) -> EmergencyRequest | None:
        """
        Закрытие заявки оператором или администратором

        Автоматически создает запись в логе с действием 'closed'.

        Args:
            request_id: ID заявки
            closed_by: ID пользователя, закрывающего заявку

        Returns:
            Обновленная заявка или None если заявка не найдена или уже закрыта
        """
        # Проверяем, что заявка существует и имеет статус 'pending'
        check_row = await self._db.fetch_one(
            "SELECT status FROM emergency_requests WHERE id = %s",
            (request_id,),
        )

        if not check_row:
            return None

        if check_row["status"] != EmergencyRequestStatus.PENDING.value:
            # Заявка уже закрыта
            return None

        # Закрываем заявку
        request_row = await self._db.fetch_one(
            """
            UPDATE emergency_requests
            SET status = 'closed',
                closed_at = now(),
                closed_by = %s
            WHERE id = %s
            RETURNING *
            """,
            (closed_by, request_id),
            read_only=False,
        )

        if not request_row:
            return None

        request = from_dict(EmergencyRequest, request_row, config=_DACITE_CONFIG)

        # Логируем закрытие
        await self._log_action(
            request_id=request_id,
            action=EmergencyRequestAction.CLOSED,
            performed_by=closed_by,
            details=None,
        )

        return request

    async def log_view(self, request_id: int, viewer_id: int) -> None:
        """Логирует просмотр заявки отдельно от выборки.

        Позволяет хендлеру сначала проверить право на просмотр (SEC-09
        scoping), а уже потом записывать факт просмотра — чтобы доступ к
        чужой заявке не оставлял ложного аудит-следа.
        """
        await self._log_action(
            request_id=request_id,
            action=EmergencyRequestAction.VIEWED,
            performed_by=viewer_id,
            details=None,
        )

    async def get_logs(
        self,
        request_id: int,
    ) -> list[EmergencyRequestLog]:
        """
        Получение истории действий с заявкой

        Args:
            request_id: ID заявки

        Returns:
            Список записей лога, отсортированных по времени (новые первые)
        """
        rows = await self._db.fetch_all(
            """
            SELECT
                id,
                request_id,
                action,
                performed_by,
                performed_at,
                details
            FROM emergency_request_logs
            WHERE request_id = %s
            ORDER BY performed_at DESC
            """,
            (request_id,),
        )

        logs = []
        for row in rows:
            # Парсим JSON details если есть
            row_dict = dict(row)
            if row_dict.get("details"):
                if isinstance(row_dict["details"], str):
                    row_dict["details"] = json.loads(row_dict["details"])
            else:
                row_dict["details"] = None

            logs.append(from_dict(EmergencyRequestLog, row_dict, config=_DACITE_CONFIG))

        return logs

    async def count(
        self,
        status: EmergencyRequestStatus | None = None,
        locker_id: str | None = None,
        user_id: int | None = None,
    ) -> int:
        """
        Подсчет количества заявок с фильтрацией

        Args:
            status: Фильтр по статусу
            locker_id: Фильтр по ID шкафа
            user_id: Фильтр по автору заявки (scoping worker'а, SEC-09).

        Returns:
            Количество заявок
        """
        query = "SELECT COUNT(*) as count FROM emergency_requests WHERE 1=1"
        params: list[SqlParam] = []

        if status is not None:
            query += " AND status = %s"
            params.append(status.value if isinstance(status, EmergencyRequestStatus) else status)

        if locker_id is not None:
            query += " AND locker_id = %s"
            params.append(locker_id)

        if user_id is not None:
            query += " AND user_id = %s"
            params.append(user_id)

        row = await self._db.fetch_one(query, tuple(params) if params else None)
        return row["count"] if row else 0

    async def _log_action(
        self,
        request_id: int,
        action: EmergencyRequestAction,
        performed_by: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        """
        Приватный метод для логирования действий с заявкой

        Args:
            request_id: ID заявки
            action: Тип действия
            performed_by: ID пользователя, выполнившего действие
            details: Дополнительные детали в виде словаря
        """
        details_json = json.dumps(details) if details else None

        await self._db.execute(
            """
            INSERT INTO emergency_request_logs (request_id, action, performed_by, details)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (request_id, action.value, performed_by, details_json),
        )
