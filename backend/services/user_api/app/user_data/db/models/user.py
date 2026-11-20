from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.enums import UserRole, UserState

__all__ = ["UserRole", "UserState", "User"]


@dataclass(slots=True)
class User:
    id: int
    phone: str
    full_name: str
    contractor_id: int | None
    contractor_name: str | None

    created_at: datetime
    created_by: int | None

    updated_at: datetime
    updated_by: int | None

    state: UserState
    role: UserRole

    expire_date: datetime | None

    # Числовой ID пользователя в боте мессенджера Max. NULL до привязки —
    # OTP-коды второго фактора уходят на этот ID.
    max_user_id: int | None = None

    # Включён ли для пользователя второй фактор (2FA). Хранится в отдельной
    # таблице user_auth (LEFT JOIN); у пользователя без записи user_auth
    # (пароль ещё не задан) флага нет → COALESCE к False. Default False на
    # уровне модели дублирует эту защиту, если ключ отсутствует в строке.
    require_2fa: bool = False
