"""
Исключения для работы с базой данных.

Содержит иерархию исключений для различных ошибок работы с БД.
"""

from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T")


class DatabaseError(Exception):
    """Базовое исключение для ошибок работы с БД"""


class QueryError(DatabaseError):
    """Ошибки выполнения запросов"""


# Специфичные для AsyncPostgres исключения

class AsyncPostgresError(DatabaseError):
    """Base exception for AsyncPostgres connector"""


class ConnectionPoolExhaustedError(AsyncPostgresError):
    """Raised when connection pool is exhausted"""


class RetryExhaustedError(AsyncPostgresError):
    """Raised when all retry attempts are exhausted"""

    def __init__(self, message: str, last_exception: Exception | None = None):
        super().__init__(message)
        self.last_exception = last_exception


class TransactionError(AsyncPostgresError):
    """Raised when transaction operation fails"""


class UnexpectedEmptyResultError(QueryError):
    """fetch_one_strict() ожидал строку, БД вернула NULL.

    Симптом race-condition (конкурентный DELETE) или сломанного WHERE —
    инвариант кода нарушен, retry не поможет.
    """

    def __init__(self, message: str, *, query: str | None = None) -> None:
        super().__init__(message)
        # Урезаем query до 500 символов чтобы не раздуть лог; params НЕ храним
        # никогда (могут содержать секреты — password_hash, OTP, JWT-payload).
        self.query = (query[:500] + "…") if query and len(query) > 500 else query


def require_found(value: T | None, *, entity: str, id_: Any) -> T:
    """Сужает `T | None` до `T` для пост-write SELECT-сайтов.

    Если значение None — INSERT/UPDATE прошёл, но follow-up SELECT не нашёл
    запись (race с DELETE либо WHERE-mismatch). Бросает QueryError, который
    `ProblemDetailsHandler.write_error` маппит в 500 RFC 7807.
    """
    if value is None:
        raise UnexpectedEmptyResultError(f"{entity} {id_!r} not found after write")
    return value
