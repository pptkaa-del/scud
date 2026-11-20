from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.reports.formats import ReportFormat


@dataclass(slots=True)
class GenerationResult:
    row_count: int


@runtime_checkable
class ReportGenerator(Protocol):
    """Расширяемый seam генерации отчётов.

    Конкретные генераторы регистрируются в registry по `report_type`.
    `validate_params` парсит/валидирует сырой dict из запроса в типизированный
    объект параметров; `generate` пишет файл и возвращает число строк.

    Параметры `validate_params`/`generate`/`serialize_params` типизированы как
    `object`, потому что Protocol-методы трактуются mypy контравариантно:
    конкретный генератор не может сузить тип параметра до своего dataclass без
    нарушения LSP. Поэтому конкретные реализации также принимают `object` и
    сужают через `isinstance`.

    `serialize_params` приводит уже провалидированный объект параметров к
    каноническому JSONB-виду для хранения в `report_jobs.params`. Хранить нужно
    именно его, а НЕ сырой dict из запроса: так в БД попадают только разобранные,
    ограниченные по форме значения, а не произвольные байты клиента.
    """

    report_type: str
    output_format: ReportFormat

    def validate_params(self, raw: dict[str, Any]) -> object: ...

    def serialize_params(self, params: object) -> dict[str, Any]: ...

    async def generate(self, params: object, out_path: Path) -> GenerationResult: ...
