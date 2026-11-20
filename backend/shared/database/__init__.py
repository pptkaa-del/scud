"""
Модуль для работы с базами данных.

Содержит общие компоненты для взаимодействия с БД:
- connection: Асинхронный коннектор AsyncPostgres (фасад модуля)
- config: PostgresConfig (frozen+slots dataclass с валидацией)
- helpers: чистые SQLSTATE-детекторы (is_unique_violation, is_foreign_key_violation)
- retry: чистая ретрай-семантика (should_retry, is_connection_broken, RETRY_SQLSTATES)
- errors: Общие исключения для работы с БД
"""

from datetime import date, datetime
from enum import Enum

from .config import PostgresConfig
from .connection import AsyncPostgres
from .errors import (
    DatabaseError,
    QueryError,
    UnexpectedEmptyResultError,
    require_found,
)
from .helpers import is_foreign_key_violation, is_unique_violation

# Типы значений, которые psycopg2 принимает как параметры SQL-запроса.
# Используется вместо `list[Any]` в repositories: новый тип параметра
# должен явно расширять SqlParam, а не прятаться за широким Any.
#
# list[str] — Postgres-массив: psycopg2 адаптирует список в array-литерал для
# `= ANY(%s::uuid[])` (фильтр по набору id, см. locker-репозиторий).
SqlParam = str | int | float | bool | datetime | date | Enum | list[str] | None

__all__ = [
    "AsyncPostgres",
    "PostgresConfig",
    "DatabaseError",
    "QueryError",
    "SqlParam",
    "UnexpectedEmptyResultError",
    "is_foreign_key_violation",
    "is_unique_violation",
    "require_found",
]
