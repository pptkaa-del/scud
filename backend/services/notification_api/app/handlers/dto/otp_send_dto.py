"""DTO для внутреннего эндпоинта `/internal/api/v1/otp/send`."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SendOtpDTO:
    """Запрос на отправку OTP-кода в Max-бот пользователю."""

    user_id: int
    code: str
    max_user_id: int | None = None
    address: str | None = None
    ttl_seconds: int = 300
    purpose: str = "otp_login"

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, int) or self.user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("code must be a non-empty string")
        if len(self.code) > 16:
            raise ValueError("code must be at most 16 characters")
        if self.max_user_id is not None and (
            not isinstance(self.max_user_id, int) or self.max_user_id <= 0
        ):
            raise ValueError("max_user_id must be a positive integer or None")
        if self.address is not None and (
            not isinstance(self.address, str) or not (1 <= len(self.address) <= 64)
        ):
            raise ValueError("address must be a 1..64 char string or None")
        if not isinstance(self.ttl_seconds, int) or self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        if self.ttl_seconds > 3600:
            raise ValueError("ttl_seconds must be at most 3600")
        if not isinstance(self.purpose, str) or not self.purpose:
            raise ValueError("purpose must be a non-empty string")
        if len(self.purpose) > 32:
            raise ValueError("purpose must be at most 32 characters")
