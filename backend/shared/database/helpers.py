"""Чистые SQLSTATE-детекторы нарушений целостности.

Позволяют repository-коду классифицировать integrity-ошибки (unique / FK), не
импортируя psycopg2 напрямую — драйвер живёт здесь, в shared.database.
"""

from __future__ import annotations

import psycopg2
import psycopg2.errors

# SQLSTATE 23505 — unique_violation. psycopg2.IntegrityError покрывает все
# нарушения целостности (FK, NOT NULL, check); сужаем до unique-violation по
# pgcode, чтобы caller-retry на конкурентной аллокации не глотал прочие
# integrity-ошибки.
_SQLSTATE_UNIQUE_VIOLATION = "23505"
# SQLSTATE 23503 — foreign_key_violation. Удаление родительской записи, на
# которую ссылается потомок (sensor → sensor_type), нарушает FK-constraint:
# это конфликт состояния ресурса (409), а не серверная ошибка (500).
_SQLSTATE_FOREIGN_KEY_VIOLATION = "23503"


def is_unique_violation(exc: Exception) -> bool:
    """True, если исключение — нарушение уникального индекса (SQLSTATE 23505).

    Позволяет repository-коду ловить unique-violation, не импортируя psycopg2
    напрямую (драйвер живёт здесь, в shared.database). Матчим двумя путями:
    psycopg2 при реальном 23505 поднимает именно подкласс
    psycopg2.errors.UniqueViolation, поэтому isinstance-проверки достаточно;
    дополнительный SQLSTATE-чек ловит общий IntegrityError, несущий pgcode
    '23505' (на случай иной обёртки драйвера). Прочие integrity-ошибки
    (FK 23503, NOT NULL 23502, check 23514) → False.
    """
    if isinstance(exc, psycopg2.errors.UniqueViolation):
        return True
    return (
        isinstance(exc, psycopg2.IntegrityError)
        and getattr(exc, "pgcode", None) == _SQLSTATE_UNIQUE_VIOLATION
    )


def is_foreign_key_violation(exc: Exception) -> bool:
    """True, если исключение — нарушение внешнего ключа (SQLSTATE 23503).

    Зеркало ``is_unique_violation`` для FK-constraint: DELETE родителя, на
    которого ссылается потомок, поднимает psycopg2.errors.ForeignKeyViolation
    (подкласс IntegrityError). Позволяет handler'у вернуть 409 вместо 500, не
    импортируя psycopg2 напрямую. Прочие integrity-ошибки (unique 23505,
    NOT NULL 23502, check 23514) → False.
    """
    if isinstance(exc, psycopg2.errors.ForeignKeyViolation):
        return True
    return (
        isinstance(exc, psycopg2.IntegrityError)
        and getattr(exc, "pgcode", None) == _SQLSTATE_FOREIGN_KEY_VIOLATION
    )
