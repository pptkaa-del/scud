"""
Модуль авторизации и аутентификации.

Содержит базовые Tornado-хендлеры и auth-mixin:
- JSONHandler: базовый HTTP-хендлер с поддержкой JSON
- ProblemDetailsHandler: RFC 7807 Problem Details поверх JSONHandler
- UserAuthMixin: извлечение пользователя из X-Remote-User header
"""

from .health import (
    HealthHandler,
    LivenessHandler,
    ReadinessCheck,
    ReadinessHandler,
    db_check,
    mqtt_check,
    redis_check,
)
from .json_handler import JSONHandler
from .problem_details import ProblemDetailsHandler
from .user_auth_mixin import UserAuthMixin

__all__ = [
    "HealthHandler",
    "JSONHandler",
    "LivenessHandler",
    "ProblemDetailsHandler",
    "ReadinessCheck",
    "ReadinessHandler",
    "UserAuthMixin",
    "db_check",
    "mqtt_check",
    "redis_check",
]
