"""IDOR-регрессия (SEC-06): отчёты скоупятся по владельцу (requested_by).

Любой OPERATOR не должен видеть/скачивать/переименовывать/удалять чужой отчёт.
Отчёты содержат ФИО + историю перемещений — закрываем горизонтальную эскалацию.

Правила:
- GET /reports (list) → только свои задачи; total не утекает чужие.
- GET/download/PATCH/DELETE /report/{id} чужого → 404 (не 403: не раскрываем
  существование чужого ресурса).
- administrator видит/трогает всё (override по X-Remote-Role).
- владелец имеет доступ к своему.

Harness `make_handler` (без HTTP-стека Tornado); репозиторий мокается через
monkeypatch ReportJobRepository в модуле handler'а.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from typing import Any
from unittest.mock import AsyncMock

from app.handlers import report_detail, report_download, reports
from app.handlers.report_detail import ReportDetailHandler
from app.handlers.report_download import ReportDownloadHandler
from app.handlers.reports import ReportListHandler
from app.reports.formats import ReportFormat
from app.reports_data.db.models.report_job import ReportJob, ReportStatus
from shared.enums import UserRole
from tests.unit.conftest import _FakeRequest, make_handler

_REPORT_ID = "11111111-2222-3333-4444-555555555555"
_OWNER_A = 7
_OWNER_B = 99


class _FakeGen:
    report_type = "locker_open"
    output_format = ReportFormat.XLSX

    def validate_params(self, raw: dict[str, Any]) -> object:
        return raw

    def serialize_params(self, params: object) -> dict[str, Any]:
        assert isinstance(params, dict)
        return params


def _make_job(**overrides: Any) -> ReportJob:
    data: dict[str, Any] = {
        "id": _REPORT_ID,
        "report_type": "locker_open",
        "name": None,
        "params": {},
        "status": ReportStatus.READY,
        "requested_by": _OWNER_B,
        "format": "xlsx",
        "file_path": "/data/reports/x.xlsx",
        "file_name": "x.xlsx",
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


def _detail_handler(
    *, user_id: int, role: str | None, body: bytes = b"", method: str = "GET"
) -> ReportDetailHandler:
    headers: dict[str, str] = {"X-Remote-User": str(user_id)}
    if role is not None:
        headers["X-Remote-Role"] = role
    request = _FakeRequest(headers=headers, body=body, method=method)
    handler = make_handler(ReportDetailHandler, {}, request=request, db=object())
    handler._current_user_id = user_id
    return handler


def _download_handler(*, user_id: int, role: str | None) -> ReportDownloadHandler:
    headers: dict[str, str] = {"X-Remote-User": str(user_id)}
    if role is not None:
        headers["X-Remote-Role"] = role
    request = _FakeRequest(headers=headers, method="GET")
    handler = make_handler(ReportDownloadHandler, {}, request=request, db=object())
    handler._current_user_id = user_id
    return handler


def _list_handler(*, user_id: int, role: str | None) -> ReportListHandler:
    headers: dict[str, str] = {"X-Remote-User": str(user_id)}
    if role is not None:
        headers["X-Remote-Role"] = role
    request = _FakeRequest(headers=headers, method="GET")
    handler = make_handler(ReportListHandler, {}, request=request, db=object())
    handler._current_user_id = user_id
    handler.initialize(handler.db, registry={"locker_open": _FakeGen()})
    return handler


# ---------------------------------------------------------------------------
# Repo получает owner-фильтр (operator) и не получает (administrator)
# ---------------------------------------------------------------------------


def test_get_passes_owner_filter_for_operator(monkeypatch):
    handler = _detail_handler(user_id=_OWNER_A, role=UserRole.OPERATOR.value)
    _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = None  # чужой → не найден в пределах скоупа
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get(_REPORT_ID))

    repo.get_by_id.assert_awaited_once()
    _, kwargs = repo.get_by_id.call_args
    assert kwargs.get("requested_by") == _OWNER_A


def test_get_admin_no_owner_filter(monkeypatch):
    handler = _detail_handler(user_id=1, role=UserRole.ADMINISTRATOR.value)
    _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = _make_job()
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get(_REPORT_ID))

    repo.get_by_id.assert_awaited_once()
    _, kwargs = repo.get_by_id.call_args
    assert kwargs.get("requested_by") is None


# ---------------------------------------------------------------------------
# GET /report/{id} — чужой → 404, свой → 200
# ---------------------------------------------------------------------------


def test_get_foreign_report_404_not_403(monkeypatch):
    handler = _detail_handler(user_id=_OWNER_A, role=UserRole.OPERATOR.value)
    captured = _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = None
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get(_REPORT_ID))

    assert captured["status"] == 404


def test_get_own_report_200(monkeypatch):
    handler = _detail_handler(user_id=_OWNER_A, role=UserRole.OPERATOR.value)
    captured = _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = _make_job(requested_by=_OWNER_A)
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get(_REPORT_ID))

    assert captured["status"] == 200


def test_get_admin_sees_foreign_200(monkeypatch):
    handler = _detail_handler(user_id=1, role=UserRole.ADMINISTRATOR.value)
    captured = _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = _make_job(requested_by=_OWNER_B)
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get(_REPORT_ID))

    assert captured["status"] == 200


# ---------------------------------------------------------------------------
# DOWNLOAD — чужой → 404
# ---------------------------------------------------------------------------


def test_download_foreign_404(monkeypatch):
    handler = _download_handler(user_id=_OWNER_A, role=UserRole.OPERATOR.value)
    captured = _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = None
    monkeypatch.setattr(report_download, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get(_REPORT_ID))

    assert captured["status"] == 404
    _, kwargs = repo.get_by_id.call_args
    assert kwargs.get("requested_by") == _OWNER_A


def test_download_admin_no_owner_filter(monkeypatch, tmp_path):
    real = tmp_path / "report.xlsx"
    real.write_bytes(b"PK\x03\x04")
    handler = _download_handler(user_id=1, role=UserRole.ADMINISTRATOR.value)
    captured = _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = _make_job(file_path=str(real))
    monkeypatch.setattr(report_download, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get(_REPORT_ID))

    assert captured["status"] == 200
    _, kwargs = repo.get_by_id.call_args
    assert kwargs.get("requested_by") is None


# ---------------------------------------------------------------------------
# PATCH (rename) — чужой → 404
# ---------------------------------------------------------------------------


def test_patch_foreign_404(monkeypatch):
    handler = _detail_handler(
        user_id=_OWNER_A,
        role=UserRole.OPERATOR.value,
        body=json.dumps({"name": "hijack"}).encode(),
        method="PATCH",
    )
    captured = _capture(handler)

    repo = AsyncMock()
    repo.update_name.return_value = None
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.patch(_REPORT_ID))

    assert captured["status"] == 404
    _, kwargs = repo.update_name.call_args
    assert kwargs.get("requested_by") == _OWNER_A


def test_patch_admin_no_owner_filter(monkeypatch):
    handler = _detail_handler(
        user_id=1,
        role=UserRole.ADMINISTRATOR.value,
        body=json.dumps({"name": "ok"}).encode(),
        method="PATCH",
    )
    captured = _capture(handler)

    repo = AsyncMock()
    repo.update_name.return_value = _make_job(name="ok")
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.patch(_REPORT_ID))

    assert captured["status"] == 200
    _, kwargs = repo.update_name.call_args
    assert kwargs.get("requested_by") is None


# ---------------------------------------------------------------------------
# DELETE — чужой → 404, не помечает expired
# ---------------------------------------------------------------------------


def test_delete_foreign_404(monkeypatch):
    handler = _detail_handler(
        user_id=_OWNER_A, role=UserRole.OPERATOR.value, method="DELETE"
    )
    captured = _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = None
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.delete(_REPORT_ID))

    assert captured["status"] == 404
    repo.mark_expired.assert_not_called()
    _, kwargs = repo.get_by_id.call_args
    assert kwargs.get("requested_by") == _OWNER_A


# ---------------------------------------------------------------------------
# LIST — owner-фильтр прокидывается в list_jobs И count_jobs (operator)
# ---------------------------------------------------------------------------


def test_list_operator_scopes_to_owner(monkeypatch):
    handler = _list_handler(user_id=_OWNER_A, role=UserRole.OPERATOR.value)
    _capture(handler)

    repo = AsyncMock()
    repo.list_jobs.return_value = []
    repo.count_jobs.return_value = 0
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get())

    _, list_kwargs = repo.list_jobs.call_args
    assert list_kwargs.get("requested_by") == _OWNER_A
    _, count_kwargs = repo.count_jobs.call_args
    assert count_kwargs.get("requested_by") == _OWNER_A


def test_list_admin_sees_all(monkeypatch):
    handler = _list_handler(user_id=1, role=UserRole.ADMINISTRATOR.value)
    _capture(handler)

    repo = AsyncMock()
    repo.list_jobs.return_value = []
    repo.count_jobs.return_value = 0
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get())

    _, list_kwargs = repo.list_jobs.call_args
    assert list_kwargs.get("requested_by") is None
    _, count_kwargs = repo.count_jobs.call_args
    assert count_kwargs.get("requested_by") is None
