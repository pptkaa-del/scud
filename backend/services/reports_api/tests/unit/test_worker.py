"""Unit-тесты для ReportWorker.

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).
Репозиторий мокается AsyncMock'ом; генератор — фейк с реальным output_format,
пишущий крошечный файл в tmp_path, чтобы `out_path.stat()` работал.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from app.reports.base import GenerationResult
from app.reports.formats import ReportFormat
from app.reports_data.db.models.report_job import ReportJob, ReportStatus
from app.services.worker import ReportWorker


def _make_job(**overrides: Any) -> ReportJob:
    data: dict[str, Any] = {
        "id": "11111111-2222-3333-4444-555555555555",
        "report_type": "locker_open",
        "name": None,
        "params": {"date_from": "2026-01-01", "date_to": "2026-02-01"},
        "status": ReportStatus.RUNNING,
        "requested_by": 7,
        "format": "xlsx",
        "file_path": None,
        "file_name": None,
        "file_size": None,
        "row_count": None,
        "error_message": None,
        "attempts": 1,
        "created_at": datetime(2026, 5, 26, 12, 0, 0),
        "started_at": None,
        "completed_at": None,
        "expires_at": None,
    }
    data.update(overrides)
    return ReportJob(**data)


class _FakeGen:
    """Генератор, пишущий крошечный файл и возвращающий row_count."""

    report_type = "locker_open"
    output_format = ReportFormat.XLSX

    def __init__(self, *, row_count: int = 3, raise_on_generate: bool = False) -> None:
        self._row_count = row_count
        self._raise = raise_on_generate

    def validate_params(self, raw: dict[str, Any]) -> object:
        return raw

    async def generate(self, params: object, out_path: Path) -> GenerationResult:
        if self._raise:
            raise RuntimeError("boom")
        out_path.write_bytes(b"xlsx-bytes")
        return GenerationResult(row_count=self._row_count)


def _worker(repo: AsyncMock, registry: dict, tmp_path: Path, *, max_attempts: int = 3) -> ReportWorker:
    return ReportWorker(
        repo=repo,
        registry=registry,
        storage_path=tmp_path,
        ttl_seconds=3600,
        max_attempts=max_attempts,
        interval_s=0.01,
    )


def test_process_one_success_marks_ready(tmp_path):
    repo = AsyncMock()
    repo.claim_pending.return_value = _make_job()
    worker = _worker(repo, {"locker_open": _FakeGen(row_count=5)}, tmp_path)

    worked = asyncio.run(worker._process_one())

    assert worked is True
    repo.mark_ready.assert_awaited_once()
    _, kwargs = repo.mark_ready.call_args
    assert kwargs["row_count"] == 5
    assert kwargs["fmt"] == "xlsx"
    assert kwargs["file_size"] == len(b"xlsx-bytes")
    assert kwargs["expires_at"] > datetime.now(UTC)
    repo.mark_failed.assert_not_called()
    repo.requeue.assert_not_called()


def test_process_one_failure_under_max_requeues(tmp_path):
    repo = AsyncMock()
    repo.claim_pending.return_value = _make_job(attempts=1)
    worker = _worker(repo, {"locker_open": _FakeGen(raise_on_generate=True)}, tmp_path, max_attempts=3)

    asyncio.run(worker._process_one())

    repo.requeue.assert_awaited_once()
    repo.mark_failed.assert_not_called()


def test_process_one_failure_at_max_marks_failed(tmp_path):
    repo = AsyncMock()
    repo.claim_pending.return_value = _make_job(attempts=3)
    worker = _worker(repo, {"locker_open": _FakeGen(raise_on_generate=True)}, tmp_path, max_attempts=3)

    asyncio.run(worker._process_one())

    repo.mark_failed.assert_awaited_once()
    repo.requeue.assert_not_called()


class _FakeGenRaising:
    """Генератор, бросающий заданное исключение в validate_params или generate."""

    report_type = "locker_open"
    output_format = ReportFormat.XLSX

    def __init__(self, *, validate_exc: Exception | None = None, generate_exc: Exception | None = None) -> None:
        self._validate_exc = validate_exc
        self._generate_exc = generate_exc

    def validate_params(self, raw: dict[str, Any]) -> object:
        if self._validate_exc is not None:
            raise self._validate_exc
        return raw

    async def generate(self, params: object, out_path: Path) -> GenerationResult:
        if self._generate_exc is not None:
            raise self._generate_exc
        out_path.write_bytes(b"xlsx-bytes")
        return GenerationResult(row_count=1)


_SECRET_TEXT = "could not connect to postgres_reports:5432"


def test_process_one_infra_failure_at_max_uses_generic_message(tmp_path):
    """Инфра-сбой (не ValueError) на последней попытке → mark_failed получает
    GENERIC-текст, сырой текст исключения (хосты/SQL) НЕ утекает владельцу."""
    repo = AsyncMock()
    repo.claim_pending.return_value = _make_job(attempts=3)
    gen = _FakeGenRaising(generate_exc=RuntimeError(_SECRET_TEXT))
    worker = _worker(repo, {"locker_open": gen}, tmp_path, max_attempts=3)

    asyncio.run(worker._process_one())

    repo.mark_failed.assert_awaited_once()
    args, _ = repo.mark_failed.call_args
    stored_message = args[1]
    assert _SECRET_TEXT not in stored_message
    assert "postgres_reports" not in stored_message
    assert stored_message == "Ошибка генерации отчёта"
    repo.requeue.assert_not_called()


def test_process_one_value_error_at_max_surfaces_message(tmp_path):
    """ValueError из validate_params (валидация пользовательского ввода) — SAFE,
    текст по-прежнему попадает владельцу."""
    repo = AsyncMock()
    repo.claim_pending.return_value = _make_job(attempts=3)
    gen = _FakeGenRaising(validate_exc=ValueError("bad param X"))
    worker = _worker(repo, {"locker_open": gen}, tmp_path, max_attempts=3)

    asyncio.run(worker._process_one())

    repo.mark_failed.assert_awaited_once()
    args, _ = repo.mark_failed.call_args
    assert args[1] == "bad param X"
    repo.requeue.assert_not_called()


def test_process_one_infra_failure_under_max_requeues(tmp_path):
    """Инфра-сбой ниже бюджета попыток — по-прежнему requeue (регресс-страж)."""
    repo = AsyncMock()
    repo.claim_pending.return_value = _make_job(attempts=1)
    gen = _FakeGenRaising(generate_exc=RuntimeError(_SECRET_TEXT))
    worker = _worker(repo, {"locker_open": gen}, tmp_path, max_attempts=3)

    asyncio.run(worker._process_one())

    repo.requeue.assert_awaited_once()
    repo.mark_failed.assert_not_called()


def test_process_one_reclaimed_over_budget_marks_failed_without_running(tmp_path):
    """Реклейм зависшей running-задачи сверх бюджета попыток → failed, без запуска.

    claim_pending инкрементит attempts при захвате; если зависшая running после
    инкремента превысила max_attempts, генератор не запускаем — задача терминально
    падает (COR-13: не зацикливать исчерпанную задачу).
    """
    repo = AsyncMock()
    # max_attempts=3, claim уже сделал attempts=4 → бюджет исчерпан.
    repo.claim_pending.return_value = _make_job(attempts=4)
    gen = _FakeGen()
    worker = _worker(repo, {"locker_open": gen}, tmp_path, max_attempts=3)

    worked = asyncio.run(worker._process_one())

    assert worked is True
    repo.mark_failed.assert_awaited_once()
    repo.mark_ready.assert_not_called()
    repo.requeue.assert_not_called()


def test_process_one_reclaimed_within_budget_runs(tmp_path):
    """Реклейм running в пределах бюджета попыток — задача перегенерируется."""
    repo = AsyncMock()
    repo.claim_pending.return_value = _make_job(attempts=2)
    worker = _worker(repo, {"locker_open": _FakeGen(row_count=4)}, tmp_path, max_attempts=3)

    asyncio.run(worker._process_one())

    repo.mark_ready.assert_awaited_once()
    repo.mark_failed.assert_not_called()


def test_process_one_unknown_report_type_marks_failed(tmp_path):
    repo = AsyncMock()
    repo.claim_pending.return_value = _make_job(report_type="ghost")
    worker = _worker(repo, {"locker_open": _FakeGen()}, tmp_path)

    worked = asyncio.run(worker._process_one())

    assert worked is True
    repo.mark_failed.assert_awaited_once()
    args, _ = repo.mark_failed.call_args
    assert "unknown report_type" in args[1]


def test_process_one_returns_false_when_no_job(tmp_path):
    repo = AsyncMock()
    repo.claim_pending.return_value = None
    worker = _worker(repo, {"locker_open": _FakeGen()}, tmp_path)

    assert asyncio.run(worker._process_one()) is False


def test_cleanup_expired_unlinks_file_and_marks(tmp_path):
    expired_file = tmp_path / "old.xlsx"
    expired_file.write_bytes(b"stale")
    repo = AsyncMock()
    repo.list_expired_ready.return_value = [
        _make_job(status=ReportStatus.READY, file_path=str(expired_file))
    ]
    worker = _worker(repo, {}, tmp_path)

    asyncio.run(worker._cleanup_expired())

    assert not expired_file.exists()
    repo.mark_expired.assert_awaited_once()


def test_cleanup_expired_missing_file_still_marks(tmp_path):
    repo = AsyncMock()
    repo.list_expired_ready.return_value = [
        _make_job(status=ReportStatus.READY, file_path=str(tmp_path / "gone.xlsx"))
    ]
    worker = _worker(repo, {}, tmp_path)

    asyncio.run(worker._cleanup_expired())

    repo.mark_expired.assert_awaited_once()
