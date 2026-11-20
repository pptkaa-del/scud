from __future__ import annotations

import logging

from shared.auth import ProblemDetailsHandler, UserAuthMixin
from shared.database import AsyncPostgres

logger = logging.getLogger(__name__)


class BaseHandler(UserAuthMixin, ProblemDetailsHandler):
    """
    Базовый обработчик для History API.

    Наследует:
    - UserAuthMixin: Аутентификация через X-Remote-User (trust boundary, первый для правильного MRO!)
    - ProblemDetailsHandler: JSON сериализация + RFC 7807 Problem Details хелперы
      (включает `get_json_body_as` для типобезопасной десериализации DTO).

    Attributes:
        db: Подключение к БД (AsyncPostgres)
        current_user_id: ID текущего пользователя из X-Remote-User header
    """

    def initialize(self, db: AsyncPostgres) -> None:
        """Инициализация handler с подключением к БД."""
        self.db = db
