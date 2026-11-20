from unittest.mock import AsyncMock

from app.reports.formats import ReportFormat
from app.reports.generators.locker_open import LockerOpenGenerator
from app.reports.registry import build_registry


def test_registry_has_locker_open():
    registry = build_registry(AsyncMock(), AsyncMock())

    assert "locker_open" in registry
    gen = registry["locker_open"]
    assert isinstance(gen, LockerOpenGenerator)
    assert gen.output_format is ReportFormat.XLSX
