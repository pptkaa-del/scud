from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class ReportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


class ReportType(StrEnum):
    LOCKER_OPEN = "locker_open"


@dataclass(slots=True)
class ReportJob:
    id: str
    report_type: str
    name: str | None
    params: dict[str, Any]
    status: ReportStatus
    requested_by: int
    format: str | None
    file_path: str | None
    file_name: str | None
    file_size: int | None
    row_count: int | None
    error_message: str | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime | None
