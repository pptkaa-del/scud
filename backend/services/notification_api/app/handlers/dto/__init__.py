# Общий paginated envelope.
from shared.dtos import PaginatedResponse

from .notification_dto import CreateNotificationDTO
from .otp_send_dto import SendOtpDTO

__all__ = ["CreateNotificationDTO", "PaginatedResponse", "SendOtpDTO"]
