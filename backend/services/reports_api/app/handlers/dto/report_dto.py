from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, overload

REPORT_NAME_MAX_LEN = 255


@overload
def normalize_report_name(raw: str) -> str: ...
@overload
def normalize_report_name(raw: None) -> None: ...
@overload
def normalize_report_name(raw: str | None) -> str | None: ...
def normalize_report_name(raw: str | None) -> str | None:
    """Обрезает и валидирует имя отчёта. None -> None (имя не задано).
    ValueError при пустом после strip или превышении длины.

    Перегрузки сохраняют точность типа: для не-None входа результат сужается до
    ``str`` (вызов из PATCH-хендлера, где имя уже проверено на None)."""
    if raw is None:
        return None
    name = raw.strip()
    if not name:
        raise ValueError("name must not be empty")
    if len(name) > REPORT_NAME_MAX_LEN:
        raise ValueError(f"name exceeds {REPORT_NAME_MAX_LEN} characters")
    return name


@dataclass
class CreateReportDTO:
    report_type: str
    params: dict[str, Any] = field(default_factory=dict)
    name: str | None = None


@dataclass
class UpdateReportDTO:
    name: str | None = None
