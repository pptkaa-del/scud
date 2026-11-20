from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class OtpCode:
    id: int
    phone: str
    code_hash: str
    attempts: int
    expires_at: datetime
    used_at: datetime | None
    created_at: datetime


@dataclass(slots=True)
class AccessToken:
    id: int
    user_id: int
    token_hash: str
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime
    user_agent: str | None
    ip_address: str | None


