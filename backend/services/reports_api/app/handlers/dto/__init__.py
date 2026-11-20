# Общий paginated envelope.
from shared.dtos import PaginatedResponse

from .report_dto import CreateReportDTO, UpdateReportDTO

__all__ = ["CreateReportDTO", "UpdateReportDTO", "PaginatedResponse"]
