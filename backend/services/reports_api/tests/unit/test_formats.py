import pytest

from app.reports.formats import ReportFormat


def test_extension_and_content_type():
    assert ReportFormat.XLSX.extension == "xlsx"
    assert "spreadsheetml" in ReportFormat.XLSX.content_type


def test_from_extension_roundtrip():
    assert ReportFormat.from_extension("xlsx") is ReportFormat.XLSX


def test_from_extension_unknown_raises():
    with pytest.raises(ValueError):
        ReportFormat.from_extension("docx")
