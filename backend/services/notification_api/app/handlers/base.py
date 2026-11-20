"""
Базовые handlers для Notification API.

Использует shared компоненты с mixins для аутентификации.
"""

import logging

from shared.auth import ProblemDetailsHandler, UserAuthMixin

logger = logging.getLogger(__name__)


class BaseHandler(UserAuthMixin, ProblemDetailsHandler):
    """
    Базовый handler для Notification API.

    Наследует:
    - UserAuthMixin: Аутентификация через X-Remote-User
    - ProblemDetailsHandler: JSON сериализация + RFC 7807 Problem Details хелперы
      (включает `get_json_body_as` для типобезопасной десериализации DTO).
    """
