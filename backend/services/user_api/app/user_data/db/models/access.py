"""Модели для управления допусками пользователей к шкафам"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(slots=True)
class UserAccess:
    """
    Базовая модель допуска пользователя

    Используется для внутренних операций с БД.
    """
    id: int
    user_id: int
    start_at: datetime
    end_at: datetime | None
    granted_by: int | None
    request_id: int | None
    full_access: bool
    is_active: bool
    created_at: datetime


@dataclass(slots=True)
class UserAccessWithLockers:
    """
    Расширенная модель допуска с информацией о доступных шкафах

    Используется для API responses, включает:
    - Информацию о пользователе, предоставившем допуск
    - Список доступных шкафов (если не full_access)
    - Свойство is_expired для проверки истечения срока
    """
    id: int
    user_id: int
    start_at: datetime
    end_at: datetime | None
    granted_by: int | None
    granted_by_full_name: str | None  # ФИО пользователя, предоставившего допуск
    request_id: int | None
    full_access: bool
    is_active: bool
    created_at: datetime
    lockers: list[str]  # Список locker_id (UUID) или пустой список если full_access=true

    @property
    def is_expired(self) -> bool:
        """
        Проверка истечения срока действия допуска

        Returns:
            True если end_at задано и прошло, False в противном случае
        """
        if self.end_at is None:
            return False
        return datetime.now(UTC) > self.end_at
