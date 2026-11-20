"""Backward-compat shim.

Исторически ``JSONHandler`` / ``ProblemDetailsHandler`` / ``UserAuthMixin``
жили в этом модуле одним файлом. Теперь они разнесены по cohesive-модулям
(``json_handler``, ``problem_details``, ``user_auth_mixin``). Импортировать
их следует из пакета ``shared.auth`` (`from shared.auth import ...`); этот
модуль оставлен только для совместимости со старыми импорт-сайтами.
"""

from .json_handler import JSONHandler
from .problem_details import ProblemDetailsHandler
from .user_auth_mixin import UserAuthMixin

__all__ = [
    "JSONHandler",
    "ProblemDetailsHandler",
    "UserAuthMixin",
]
