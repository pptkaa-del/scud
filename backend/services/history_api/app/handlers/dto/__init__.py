"""
Data Transfer Objects (DTO) для History API handlers.

Dataclass модели для request bodies и responses, синхронизированные с OpenAPI схемами.
Используются для типобезопасной работы с JSON через dacite.from_dict().
"""

# Request DTOs
# Response DTOs — общие
from shared.dtos import PaginatedResponse

# RFC 7807 Problem Details — единая реализация для всех сервисов
from shared.http import ProblemDetails, ProblemTitle, ProblemType

from .command_history_dto import CommandHistoryCreateDTO

# Response DTOs — локальные
from .responses import BatchInsertResponse
from .security_event_dto import SecurityEventCreateDTO
from .telemetry_dto import TelemetryBatchRequestDTO, TelemetryReadingDTO

__all__ = [
    # Request DTOs
    'SecurityEventCreateDTO',
    'CommandHistoryCreateDTO',
    'TelemetryBatchRequestDTO',
    'TelemetryReadingDTO',
    # Response DTOs
    'ProblemDetails',
    'PaginatedResponse',
    'BatchInsertResponse',
    # Problem Types
    'ProblemType',
    'ProblemTitle',
]
