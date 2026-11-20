# Общий paginated envelope — один источник истины в shared.
from shared.dtos import PaginatedResponse

from .locker_dto import BatchLockerRequestDTO

__all__ = ["BatchLockerRequestDTO", "PaginatedResponse"]
