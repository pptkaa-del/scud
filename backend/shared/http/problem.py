"""
RFC 7807 Problem Details — единая реализация для всех сервисов СКУД.

Содержит:
- `ProblemDetails` — dataclass под спецификацию RFC 7807
- `ProblemType` — URI-константы для типов проблем
- `ProblemTitle` — соответствующие человекочитаемые заголовки

Спецификация: https://datatracker.ietf.org/doc/html/rfc7807
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class ProblemDetails:
    """RFC 7807 Problem Details for HTTP APIs."""

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


class ProblemType:
    """URI-константы для типов проблем согласно RFC 7807."""

    VALIDATION_ERROR = "https://api.scuddocs.com/problems/validation-error"
    INVALID_JSON = "https://api.scuddocs.com/problems/invalid-json"
    UNAUTHORIZED = "https://api.scuddocs.com/problems/unauthorized"
    FORBIDDEN = "https://api.scuddocs.com/problems/forbidden"
    NOT_FOUND = "https://api.scuddocs.com/problems/not-found"
    CONFLICT = "https://api.scuddocs.com/problems/conflict"
    PAYLOAD_TOO_LARGE = "https://api.scuddocs.com/problems/payload-too-large"
    UNSUPPORTED_MEDIA_TYPE = "https://api.scuddocs.com/problems/unsupported-media-type"
    TOO_MANY_REQUESTS = "https://api.scuddocs.com/problems/too-many-requests"
    INTERNAL_ERROR = "https://api.scuddocs.com/problems/internal-error"
    DATABASE_ERROR = "https://api.scuddocs.com/problems/database-error"
    SERVICE_UNAVAILABLE = "https://api.scuddocs.com/problems/service-unavailable"
    MAX_BINDING_REQUIRED = "https://api.scuddocs.com/problems/max-binding-required"


class ProblemTitle:
    """Стандартные заголовки, парные ProblemType."""

    VALIDATION_ERROR = "Validation Error"
    INVALID_JSON = "Invalid JSON"
    UNAUTHORIZED = "Unauthorized"
    FORBIDDEN = "Forbidden"
    NOT_FOUND = "Not Found"
    METHOD_NOT_ALLOWED = "Method Not Allowed"
    CONFLICT = "Conflict"
    PAYLOAD_TOO_LARGE = "Payload Too Large"
    UNSUPPORTED_MEDIA_TYPE = "Unsupported Media Type"
    TOO_MANY_REQUESTS = "Too Many Requests"
    INTERNAL_ERROR = "Internal Server Error"
    DATABASE_ERROR = "Database Error"
    SERVICE_UNAVAILABLE = "Service Unavailable"
    MAX_BINDING_REQUIRED = "Max Binding Required"


# Маппинг HTTP-статусов на (ProblemType, ProblemTitle) для fallback-ветки
# `ProblemDetailsHandler.write_error`. Используется когда Tornado зовёт
# write_error без exc_info (роутер не нашёл маршрут, method not allowed
# и т.п.) — мы не должны отдавать INTERNAL_ERROR с кодом 404.
STATUS_TO_PROBLEM: dict[int, tuple[str, str]] = {
    400: (ProblemType.VALIDATION_ERROR, ProblemTitle.VALIDATION_ERROR),
    401: (ProblemType.UNAUTHORIZED, ProblemTitle.UNAUTHORIZED),
    403: (ProblemType.FORBIDDEN, ProblemTitle.FORBIDDEN),
    404: (ProblemType.NOT_FOUND, ProblemTitle.NOT_FOUND),
    405: (ProblemType.INTERNAL_ERROR, ProblemTitle.METHOD_NOT_ALLOWED),
    409: (ProblemType.CONFLICT, ProblemTitle.CONFLICT),
    413: (ProblemType.PAYLOAD_TOO_LARGE, ProblemTitle.PAYLOAD_TOO_LARGE),
    415: (ProblemType.UNSUPPORTED_MEDIA_TYPE, ProblemTitle.UNSUPPORTED_MEDIA_TYPE),
    429: (ProblemType.TOO_MANY_REQUESTS, ProblemTitle.TOO_MANY_REQUESTS),
    503: (ProblemType.SERVICE_UNAVAILABLE, ProblemTitle.SERVICE_UNAVAILABLE),
}
