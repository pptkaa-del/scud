"""
Общие DTO для всех сервисов backend.

Содержит структуры данных, переиспользуемые между сервисами:
- PaginatedResponse — единый формат paginated list-ответов
  (`{items, total, limit, offset}`).

См. `backend/CONVENTIONS.md` раздел «Пагинированные list-ответы».
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class PaginatedResponse(Generic[T]):
    """
    Стандартный envelope для paginated list-эндпоинтов.

    Все list-эндпоинты публичного и internal API обязаны возвращать
    объект с этими 4 полями. `limit` и `offset` всегда echo-ятся
    из query (или их default'ов); `total` — общее число записей,
    удовлетворяющих фильтру (без учёта пагинации).

    Attributes:
        items: список элементов текущей страницы (любой тип, обычно dataclass)
        total: общее количество элементов с применёнными фильтрами
        limit: максимальное число элементов на странице (echo от query)
        offset: смещение от начала (echo от query)

    Example:
        >>> PaginatedResponse(items=[u1, u2], total=542, limit=100, offset=0)
    """

    items: list[T]
    total: int
    limit: int
    offset: int
