"""Unit-тесты для ReportListHandler / ReportDetailHandler.

Используется harness `make_handler` (без HTTP-стека Tornado), как в test_health.
Репозиторий мокается через monkeypatch ReportJobRepository в модуле handler'а.
"""
import asyncio
from datetime import datetime
import json
from typing import Any
from unittest.mock import AsyncMock

from app.handlers import report_detail, reports
from app.handlers.report_detail import ReportDetailHandler
from app.handlers.reports import ReportListHandler
from app.reports.formats import ReportFormat
from app.reports_data.db.models.report_job import ReportJob, ReportStatus
from tests.unit.conftest import _FakeRequest, make_handler

_BAD_PARAMS = {"bad": True}


class _FakeGen:
    """Минимальный ReportGenerator-стенд для handler-тестов."""

    report_type = "locker_open"
    output_format = ReportFormat.XLSX

    def validate_params(self, raw: dict[str, Any]) -> object:
        if raw == _BAD_PARAMS:
            raise ValueError("bad params")
        return raw

    def serialize_params(self, params: object) -> dict[str, Any]:
        # Identity: для большинства handler-тестов хранимый вид == входному.
        assert isinstance(params, dict)
        return params


class _CanonGen:
    """Генератор-стенд, чей serialize_params возвращает канонический вид,
    отличный от сырого ввода — проверяем, что handler хранит именно его."""

    report_type = "locker_open"
    output_format = ReportFormat.XLSX

    def validate_params(self, raw: dict[str, Any]) -> object:
        return raw

    def serialize_params(self, params: object) -> dict[str, Any]:
        return {"date_from": "2026-01-01T00:00:00"}


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


def _make_job(**overrides: Any) -> ReportJob:
    data: dict[str, Any] = {
        "id": "11111111-2222-3333-4444-555555555555",
        "report_type": "locker_open",
        "name": None,
        "params": {},
        "status": ReportStatus.PENDING,
        "requested_by": 7,
        "format": "xlsx",
        "file_path": None,
        "file_name": None,
        "file_size": None,
        "row_count": None,
        "error_message": None,
        "attempts": 0,
        "created_at": datetime(2026, 5, 26, 12, 0, 0),
        "started_at": None,
        "completed_at": None,
        "expires_at": None,
    }
    data.update(overrides)
    return ReportJob(**data)


def _list_handler(
    body: bytes = b"", query: dict | None = None, gen: Any | None = None
) -> ReportListHandler:
    request = _FakeRequest(
        headers={"X-Remote-User": "7"},
        body=body,
        method="POST" if body else "GET",
        query_arguments=query or {},
    )
    handler = make_handler(ReportListHandler, {}, request=request, db=object())
    handler._current_user_id = 7
    handler.initialize(handler.db, registry={"locker_open": gen or _FakeGen()})
    return handler


# ---------------------------------------------------------------------------
# POST /api/v1/reports
# ---------------------------------------------------------------------------


def test_post_unknown_report_type_400():
    handler = _list_handler(body=json.dumps({"report_type": "ghost"}).encode())
    captured = _capture(handler)

    asyncio.run(handler.post())

    assert captured["status"] == 400
    assert "unknown report_type" in json.loads(captured["body"])["detail"]


def test_post_bad_params_400(monkeypatch):
    handler = _list_handler(
        body=json.dumps({"report_type": "locker_open", "params": _BAD_PARAMS}).encode()
    )
    captured = _capture(handler)

    repo = AsyncMock()
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.post())

    assert captured["status"] == 400
    assert "bad params" in json.loads(captured["body"])["detail"]
    # Defense-in-depth: отвергнутые params не доходят до БД.
    repo.create.assert_not_called()


def test_post_persists_serialized_params_not_raw(monkeypatch):
    """Handler пишет в БД канонический serialize_params(validated), а не сырой
    dto.params — произвольные значения клиента не персистятся дословно."""
    raw = {"date_from": "2026-01-01", "date_to": "2026-02-01"}
    handler = _list_handler(
        body=json.dumps({"report_type": "locker_open", "params": raw}).encode(),
        gen=_CanonGen(),
    )
    _capture(handler)

    repo = AsyncMock()
    repo.create.return_value = _make_job(params={"date_from": "2026-01-01T00:00:00"})
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.post())

    repo.create.assert_awaited_once()
    _, kwargs = repo.create.call_args
    assert kwargs["params"] == {"date_from": "2026-01-01T00:00:00"}
    assert kwargs["params"] != raw


def test_post_ok_202_and_create_called(monkeypatch):
    params = {"date_from": "2026-01-01"}
    handler = _list_handler(
        body=json.dumps({"report_type": "locker_open", "params": params}).encode()
    )
    captured = _capture(handler)

    repo = AsyncMock()
    repo.create.return_value = _make_job(params=params)
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.post())

    assert captured["status"] == 202
    body = json.loads(captured["body"])
    assert body["id"] == "11111111-2222-3333-4444-555555555555"
    assert body["status"] == "pending"
    assert body["format"] == "xlsx"

    repo.create.assert_awaited_once()
    _, kwargs = repo.create.call_args
    assert kwargs["requested_by"] == 7
    assert kwargs["report_type"] == "locker_open"
    assert kwargs["params"] == params
    assert kwargs["fmt"] == "xlsx"


def test_post_with_name_passes_name_to_create(monkeypatch):
    params = {"date_from": "2026-01-01"}
    handler = _list_handler(
        body=json.dumps(
            {"report_type": "locker_open", "params": params, "name": "  Quarterly  "}
        ).encode()
    )
    captured = _capture(handler)

    repo = AsyncMock()
    repo.create.return_value = _make_job(params=params, name="Quarterly")
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.post())

    assert captured["status"] == 202
    body = json.loads(captured["body"])
    assert body["name"] == "Quarterly"

    repo.create.assert_awaited_once()
    _, kwargs = repo.create.call_args
    assert kwargs["name"] == "Quarterly"


def test_post_without_name_passes_none(monkeypatch):
    params = {"date_from": "2026-01-01"}
    handler = _list_handler(
        body=json.dumps({"report_type": "locker_open", "params": params}).encode()
    )
    _capture(handler)

    repo = AsyncMock()
    repo.create.return_value = _make_job(params=params)
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.post())

    _, kwargs = repo.create.call_args
    assert kwargs["name"] is None


def test_post_empty_name_400(monkeypatch):
    handler = _list_handler(
        body=json.dumps({"report_type": "locker_open", "name": "   "}).encode()
    )
    captured = _capture(handler)

    repo = AsyncMock()
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.post())

    assert captured["status"] == 400
    repo.create.assert_not_called()


def test_post_name_too_long_400(monkeypatch):
    handler = _list_handler(
        body=json.dumps(
            {"report_type": "locker_open", "name": "x" * 256}
        ).encode()
    )
    captured = _capture(handler)

    repo = AsyncMock()
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.post())

    assert captured["status"] == 400
    repo.create.assert_not_called()


# ---------------------------------------------------------------------------
# GET /api/v1/reports
# ---------------------------------------------------------------------------


def test_get_list_200_paginated(monkeypatch):
    handler = _list_handler()
    captured = _capture(handler)

    repo = AsyncMock()
    repo.list_jobs.return_value = [_make_job()]
    repo.count_jobs.return_value = 1
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get())

    assert captured["status"] == 200
    body = json.loads(captured["body"])
    assert body["total"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 1


def test_get_list_passes_q_filter(monkeypatch):
    handler = _list_handler(query={"q": [b"quarter"]})
    _capture(handler)

    repo = AsyncMock()
    repo.list_jobs.return_value = [_make_job()]
    repo.count_jobs.return_value = 1
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get())

    repo.list_jobs.assert_awaited_once()
    _, list_kwargs = repo.list_jobs.call_args
    assert list_kwargs["q"] == "quarter"

    repo.count_jobs.assert_awaited_once()
    _, count_kwargs = repo.count_jobs.call_args
    assert count_kwargs["q"] == "quarter"


def test_get_list_q_too_long_400(monkeypatch):
    handler = _list_handler(query={"q": [b"x" * 256]})
    captured = _capture(handler)

    repo = AsyncMock()
    monkeypatch.setattr(reports, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get())

    assert captured["status"] == 400
    repo.list_jobs.assert_not_called()


# ---------------------------------------------------------------------------
# GET /api/v1/report/{id}
# ---------------------------------------------------------------------------


def test_detail_unknown_404(monkeypatch):
    handler = make_handler(ReportDetailHandler, {}, db=object())
    handler._current_user_id = 7
    captured = _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = None
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get("deadbeef"))

    assert captured["status"] == 404


def test_detail_known_200(monkeypatch):
    handler = make_handler(ReportDetailHandler, {}, db=object())
    handler._current_user_id = 7
    captured = _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = _make_job()
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.get("11111111-2222-3333-4444-555555555555"))

    assert captured["status"] == 200
    body = json.loads(captured["body"])
    assert body["id"] == "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# DELETE /api/v1/report/{id}
# ---------------------------------------------------------------------------


def test_delete_unknown_404(monkeypatch):
    handler = make_handler(ReportDetailHandler, {}, db=object())
    handler._current_user_id = 7
    captured = _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = None
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.delete("deadbeef"))

    assert captured["status"] == 404
    repo.mark_expired.assert_not_called()


def test_delete_known_204_marks_expired(monkeypatch, tmp_path):
    real = tmp_path / "report.xlsx"
    real.write_bytes(b"data")
    handler = make_handler(ReportDetailHandler, {}, db=object())
    handler._current_user_id = 7
    captured = _capture(handler)

    repo = AsyncMock()
    repo.get_by_id.return_value = _make_job(file_path=str(real))
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.delete("11111111-2222-3333-4444-555555555555"))

    assert captured["status"] == 204
    assert not real.exists()
    repo.mark_expired.assert_awaited_once()


# ---------------------------------------------------------------------------
# PATCH /api/v1/report/{id}
# ---------------------------------------------------------------------------


def _detail_handler(body: bytes) -> ReportDetailHandler:
    request = _FakeRequest(
        headers={"X-Remote-User": "7"},
        body=body,
        method="PATCH",
    )
    handler = make_handler(ReportDetailHandler, {}, request=request, db=object())
    handler._current_user_id = 7
    return handler


def test_patch_renames_200(monkeypatch):
    handler = _detail_handler(json.dumps({"name": "New name"}).encode())
    captured = _capture(handler)

    repo = AsyncMock()
    repo.update_name.return_value = _make_job(name="New name")
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.patch("11111111-2222-3333-4444-555555555555"))

    assert captured["status"] == 200
    body = json.loads(captured["body"])
    assert body["name"] == "New name"
    # Owner-scoping (SEC-06): мутация скоупится по владельцу (requested_by=7).
    repo.update_name.assert_awaited_once_with(
        "11111111-2222-3333-4444-555555555555", "New name", requested_by=7
    )


def test_patch_unknown_404(monkeypatch):
    handler = _detail_handler(json.dumps({"name": "New name"}).encode())
    captured = _capture(handler)

    repo = AsyncMock()
    repo.update_name.return_value = None
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.patch("deadbeef"))

    assert captured["status"] == 404


def test_patch_missing_name_400(monkeypatch):
    handler = _detail_handler(json.dumps({}).encode())
    captured = _capture(handler)

    repo = AsyncMock()
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.patch("11111111-2222-3333-4444-555555555555"))

    assert captured["status"] == 400
    repo.update_name.assert_not_called()


def test_patch_empty_name_400(monkeypatch):
    handler = _detail_handler(json.dumps({"name": "  "}).encode())
    captured = _capture(handler)

    repo = AsyncMock()
    monkeypatch.setattr(report_detail, "ReportJobRepository", lambda db: repo)

    asyncio.run(handler.patch("11111111-2222-3333-4444-555555555555"))

    assert captured["status"] == 400
    repo.update_name.assert_not_called()
