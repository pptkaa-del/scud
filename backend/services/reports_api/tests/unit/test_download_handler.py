"""Unit-тесты для ReportDownloadHandler.

Harness `make_handler` (без HTTP-стека Tornado); репозиторий мокается через
monkeypatch ReportJobRepository в модуле handler'а.
Статусы: 409 (pending/running), 410 (expired/failed/файл отсутствует),
404 (нет задачи), 200 (готов, с Content-Type/Content-Disposition).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.handlers import report_download
from app.handlers.report_download import ReportDownloadHandler
from app.reports.formats import ReportFormat
from app.reports_data.db.models.report_job import ReportJob, ReportStatus
from tests.unit.conftest import make_handler

_REPORT_ID = "11111111-2222-3333-4444-555555555555"


def _make_job(**overrides: Any) -> ReportJob:
    data: dict[str, Any] = {
        "id": _REPORT_ID,
        "report_type": "locker_open",
        "name": None,
        "params": {},
        "status": ReportStatus.READY,
        "requested_by": 7,
        "format": "xlsx",
        "file_path": "/data/reports/x.xlsx",
        "file_name": "locker_open_20260526.xlsx",
        "file_size": 10,
        "row_count": 3,
        "error_message": None,
        "attempts": 1,
        "created_at": datetime(2026, 5, 26, 12, 0, 0),
        "started_at": None,
        "completed_at": None,
        "expires_at": None,
    }
    data.update(overrides)
    return ReportJob(**data)


def _capture(handler) -> dict:
    captured: dict[str, Any] = {"status": None, "body": None, "headers": {}}

    def fake_set_status(code: int, reason: str | None = None) -> None:
        captured["status"] = code
        handler._status_code = code

    def fake_write(chunk: Any) -> None:
        captured["body"] = chunk

    def fake_set_header(name: str, value: Any) -> None:
        captured["headers"][name] = value

    handler.set_status = fake_set_status
    handler.write = fake_write
    handler.set_header = fake_set_header
    return captured


def _handler():
    handler = make_handler(ReportDownloadHandler, {}, db=object())
    handler._current_user_id = 7
    return handler


def _patch_repo(monkeypatch, job: ReportJob | None) -> None:
    repo = AsyncMock()
    repo.get_by_id.return_value = job
    monkeypatch.setattr(report_download, "ReportJobRepository", lambda db: repo)


def test_download_unknown_404(monkeypatch):
    handler = _handler()
    captured = _capture(handler)
    _patch_repo(monkeypatch, None)

    asyncio.run(handler.get(_REPORT_ID))

    assert captured["status"] == 404


@pytest.mark.parametrize("status", [ReportStatus.PENDING, ReportStatus.RUNNING])
def test_download_not_ready_409(monkeypatch, status):
    handler = _handler()
    captured = _capture(handler)
    _patch_repo(monkeypatch, _make_job(status=status))

    asyncio.run(handler.get(_REPORT_ID))

    assert captured["status"] == 409


@pytest.mark.parametrize("status", [ReportStatus.EXPIRED, ReportStatus.FAILED])
def test_download_expired_or_failed_410(monkeypatch, status):
    handler = _handler()
    captured = _capture(handler)
    _patch_repo(monkeypatch, _make_job(status=status))

    asyncio.run(handler.get(_REPORT_ID))

    assert captured["status"] == 410


def test_download_ready_but_missing_file_410(monkeypatch, tmp_path):
    handler = _handler()
    captured = _capture(handler)
    _patch_repo(monkeypatch, _make_job(file_path=str(tmp_path / "gone.xlsx")))

    asyncio.run(handler.get(_REPORT_ID))

    assert captured["status"] == 410


def test_download_ready_200_streams_file(monkeypatch, tmp_path):
    real = tmp_path / "report.xlsx"
    real.write_bytes(b"PK\x03\x04fake-xlsx")
    handler = _handler()
    captured = _capture(handler)
    _patch_repo(
        monkeypatch,
        _make_job(file_path=str(real), file_name="locker_open_20260526.xlsx"),
    )

    asyncio.run(handler.get(_REPORT_ID))

    assert captured["status"] == 200
    assert captured["body"] == b"PK\x03\x04fake-xlsx"
    assert (
        captured["headers"]["Content-Type"]
        == ReportFormat.XLSX.content_type
    )
    assert (
        captured["headers"]["Content-Disposition"]
        == 'attachment; filename="locker_open_20260526.xlsx"'
    )
