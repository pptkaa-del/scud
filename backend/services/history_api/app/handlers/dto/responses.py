"""
Response DTOs для History API.

Локальные типизированные ответы. Общий paginated envelope —
`shared.dtos.PaginatedResponse`. RFC 7807 Problem Details —
общий компонент в `shared.http.problem`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BatchInsertResponse:
    """
    Ответ для batch-операций вставки.

    Используется для телеметрии и других batch эндпоинтов.

    Attributes:
        inserted: Количество успешно вставленных записей

    Example:
        >>> response = BatchInsertResponse(inserted=150)
    """
    inserted: int
