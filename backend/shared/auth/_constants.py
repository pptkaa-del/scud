"""Приватные константы и TypeVar'ы, разделяемые модулями ``shared.auth``.

Подмодуль с underscore-префиксом: НЕ часть публичного API ``shared.auth``
(не реэкспортируется из ``__init__``). Содержит generic, client-facing
detail-строки для internal-failure классов (SEC-23) и общие TypeVar'ы.
"""

from enum import Enum
from typing import TypeVar

T = TypeVar("T")
E = TypeVar("E", bound=Enum)

# Generic, client-facing detail-строки для internal-failure классов (SEC-23).
# Внутренний текст исключения (Docker-хосты/URL, SQL, psycopg2-сообщения, raw
# баг-message) НИКОГДА не уходит клиенту в detail/title — только в серверный
# лог. Эти строки безопасны для показа конечному пользователю через gateway.
DETAIL_UPSTREAM_UNAVAILABLE = "upstream service unavailable"
DETAIL_DATABASE_UNAVAILABLE = "database unavailable"
DETAIL_DATABASE_ERROR = "database error"
DETAIL_INTERNAL_ERROR = "internal error"

# Шаблон detail для невалидного целочисленного path/строкового аргумента
# (get_int_path_arg). Сырое значение НЕ подставляется в detail — только имя
# поля + generic-текст, чтобы не эхо-ить потенциально вредоносный ввод.
INT_ARG_ERROR_TEMPLATE = "{field} must be an integer"
