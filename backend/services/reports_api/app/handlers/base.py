"""
Базовые handlers для Reports API.

Использует shared компоненты с mixins для аутентификации.
"""

import logging

from shared.auth import ProblemDetailsHandler, UserAuthMixin
from shared.enums import UserRole

logger = logging.getLogger(__name__)


class BaseHandler(UserAuthMixin, ProblemDetailsHandler):
    """
    Базовый handler для Reports API.

    Наследует:
    - UserAuthMixin: Аутентификация через X-Remote-User
    - ProblemDetailsHandler: JSON сериализация + RFC 7807 Problem Details хелперы
      (включает `get_json_body_as` для типобезопасной десериализации DTO).
    """

    def is_administrator(self) -> bool:
        """Роль вызывающего — administrator (из доверенного X-Remote-Role).

        Заголовок проставляет gateway после JWT-валидации; downstream доверяет.
        Сравнение регистронезависимое — как в locker_api command handler.
        """
        role: str = self.remote_role.strip().lower()
        return role == UserRole.ADMINISTRATOR.value

    def owner_scope(self) -> int | None:
        """Фильтр владельца для запросов к report_jobs (защита от IDOR, SEC-06).

        Возвращает id текущего пользователя для обычных ролей (operator/worker) —
        они видят/трогают только свои отчёты; ``None`` для администратора —
        снимает фильтр, давая доступ ко всем отчётам.
        """
        return None if self.is_administrator() else self.current_user_id
