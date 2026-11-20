from __future__ import annotations

from enum import Enum


class ReportFormat(Enum):
    XLSX = ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    CSV = ("csv", "text/csv; charset=utf-8")
    PDF = ("pdf", "application/pdf")

    def __init__(self, extension: str, content_type: str) -> None:
        self.extension = extension
        self.content_type = content_type

    @classmethod
    def from_extension(cls, ext: str) -> ReportFormat:
        for member in cls:
            if member.extension == ext:
                return member
        raise ValueError(f"unknown report format extension: {ext}")
