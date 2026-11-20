"""
Data Transfer Objects (DTO) для User API handlers.

Dataclass модели для request bodies и responses, синхронизированные с OpenAPI схемами.
Используются для типобезопасной работы с JSON через dacite.from_dict().
"""

# Request DTOs
from app.handlers.dto.access_dto import BatchAccessRequestDTO, CreateUserAccessDTO, UpdateUserAccessDTO
from app.handlers.dto.contractor_dto import CreateContractorDTO, UpdateContractorDTO
from app.handlers.dto.emergency_request_dto import CreateEmergencyRequestDTO
from app.handlers.dto.user_dto import CreateUserDTO, UpdateUserDTO

# Response DTOs — общие
from shared.dtos import PaginatedResponse

# RFC 7807 Problem Details — единая реализация для всех сервисов
from shared.http import ProblemDetails, ProblemTitle, ProblemType

__all__ = [
    # Request DTOs
    'CreateUserDTO',
    'UpdateUserDTO',
    'CreateContractorDTO',
    'UpdateContractorDTO',
    'CreateUserAccessDTO',
    'UpdateUserAccessDTO',
    'BatchAccessRequestDTO',
    'CreateEmergencyRequestDTO',
    # Response DTOs
    'ProblemDetails',
    'PaginatedResponse',
    # Problem Types
    'ProblemType',
    'ProblemTitle',
]
