"""Модели данных для модуля работы с заявками на экстренное открытие шкафов"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class EmergencyRequestStatus(StrEnum):
    """Статус заявки на экстренное открытие"""
    PENDING = "pending"  # Ожидает обработки
    CLOSED = "closed"    # Закрыта оператором/администратором


class EmergencyRequestAction(StrEnum):
    """Тип действия с заявкой для логирования"""
    CREATED = "created"  # Заявка создана
    CLOSED = "closed"    # Заявка закрыта
    VIEWED = "viewed"    # Заявка просмотрена


@dataclass(slots=True)
class EmergencyRequest:
    """
    Заявка на экстренное открытие шкафа

    Attributes:
        id: Уникальный идентификатор заявки
        user_id: ID пользователя, подавшего заявку
        locker_id: ID шкафа (из внешнего сервиса)
        status: Текущий статус заявки
        created_at: Дата и время создания
        closed_at: Дата и время закрытия (если закрыта)
        closed_by: ID пользователя, закрывшего заявку (оператор/администратор)
    """
    id: int
    user_id: int
    locker_id: str
    status: EmergencyRequestStatus

    created_at: datetime
    closed_at: datetime | None
    closed_by: int | None


@dataclass(slots=True)
class EmergencyRequestWithDetails:
    """
    Расширенная модель заявки с деталями пользователей

    Включает информацию о пользователе, подавшем заявку,
    и пользователе, закрывшем её (если применимо).

    Attributes:
        id: Уникальный идентификатор заявки
        user_id: ID пользователя, подавшего заявку
        user_full_name: ФИО пользователя, подавшего заявку
        user_phone: Телефон пользователя
        locker_id: ID шкафа
        status: Текущий статус заявки
        created_at: Дата и время создания
        closed_at: Дата и время закрытия
        closed_by: ID пользователя, закрывшего заявку
        closed_by_full_name: ФИО пользователя, закрывшего заявку
    """
    id: int
    user_id: int
    user_full_name: str
    user_phone: str
    locker_id: str
    status: EmergencyRequestStatus

    created_at: datetime
    closed_at: datetime | None
    closed_by: int | None
    closed_by_full_name: str | None


@dataclass(slots=True)
class EmergencyRequestLog:
    """
    Запись в логе действий с заявкой (audit trail)

    Attributes:
        id: Уникальный идентификатор записи лога
        request_id: ID заявки
        action: Тип действия
        performed_by: ID пользователя, выполнившего действие
        performed_at: Дата и время выполнения
        details: Дополнительные детали (JSON)
    """
    id: int
    request_id: int
    action: EmergencyRequestAction
    performed_by: int
    performed_at: datetime
    details: dict[str, Any] | None
