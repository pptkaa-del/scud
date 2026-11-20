"""Unit-тесты для ReportJobRepository.

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).
БД мокается через `db_mock` (AsyncMock на fetch_one/fetch_one_strict/fetch_all).
"""
import asyncio
from datetime import datetime
import json
from typing import Any

from app.reports_data.db.models.report_job import ReportJob, ReportStatus
from app.reports_data.db.repositories.report_job import ReportJobRepository


def _full_row(**overrides: Any) -> dict[str, Any]:
    """Полная строка report_jobs со всеми полями ReportJob."""
    row: dict[str, Any] = {
        "id": "11111111-2222-3333-4444-555555555555",
        "report_type": "locker_open",
        "name": None,
        "params": {"date_from": "2026-01-01"},
        "status": "pending",
        "requested_by": 42,
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
    row.update(overrides)
    return row


def test_create_inserts_and_returns_model(db_mock):
    db_mock.fetch_one_strict.return_value = _full_row()
    repo = ReportJobRepository(db_mock)

    params = {"date_from": "2026-01-01", "date_to": "2026-02-01"}
    job = asyncio.run(
        repo.create(
            report_type="locker_open",
            params=params,
            requested_by=42,
            fmt="xlsx",
        )
    )

    assert isinstance(job, ReportJob)
    assert isinstance(job.id, str)
    assert job.status is ReportStatus.PENDING

    db_mock.fetch_one_strict.assert_awaited_once()
    args, kwargs = db_mock.fetch_one_strict.call_args
    sql = args[0]
    sql_params = args[1]
    assert "INSERT INTO report_jobs" in sql
    assert "RETURNING *" in sql
    # params сериализуются через json.dumps; name по умолчанию None.
    assert sql_params == ("locker_open", None, json.dumps(params), 42, "xlsx")
    assert kwargs.get("read_only") is False


def test_create_passes_name(db_mock):
    db_mock.fetch_one_strict.return_value = _full_row(name="My report")
    repo = ReportJobRepository(db_mock)

    params = {"date_from": "2026-01-01"}
    asyncio.run(
        repo.create(
            report_type="locker_open",
            params=params,
            requested_by=42,
            fmt="xlsx",
            name="My report",
        )
    )

    args, _ = db_mock.fetch_one_strict.call_args
    sql_params = args[1]
    assert sql_params[1] == "My report"


def test_update_name_returns_model(db_mock):
    db_mock.fetch_one.return_value = _full_row(name="Renamed")
    repo = ReportJobRepository(db_mock)

    job = asyncio.run(repo.update_name("id-1", "Renamed"))

    assert job is not None
    assert job.name == "Renamed"
    args, kwargs = db_mock.fetch_one.call_args
    sql, params = args[0], args[1]
    assert "UPDATE report_jobs SET name = %s" in sql
    assert "RETURNING *" in sql
    assert params == ("Renamed", "id-1")
    assert kwargs.get("read_only") is False


def test_update_name_returns_none_when_missing(db_mock):
    db_mock.fetch_one.return_value = None
    repo = ReportJobRepository(db_mock)
    assert asyncio.run(repo.update_name("nope", "x")) is None


def test_to_model_handles_params_as_dict():
    job = ReportJobRepository._to_model(_full_row(params={"k": "v"}))
    assert job.params == {"k": "v"}
    assert isinstance(job.id, str)
    assert job.status is ReportStatus.PENDING


def test_to_model_handles_params_as_json_string():
    job = ReportJobRepository._to_model(_full_row(params=json.dumps({"k": "v"})))
    assert job.params == {"k": "v"}


def test_to_model_casts_status_enum():
    job = ReportJobRepository._to_model(_full_row(status="ready"))
    assert job.status is ReportStatus.READY


def test_get_by_id_returns_none_when_missing(db_mock):
    db_mock.fetch_one.return_value = None
    repo = ReportJobRepository(db_mock)
    assert asyncio.run(repo.get_by_id("nope")) is None


def test_get_by_id_returns_model(db_mock):
    db_mock.fetch_one.return_value = _full_row()
    repo = ReportJobRepository(db_mock)
    job = asyncio.run(repo.get_by_id("11111111-2222-3333-4444-555555555555"))
    assert job is not None
    assert job.report_type == "locker_open"


def test_list_jobs_builds_where_with_filters(db_mock):
    db_mock.fetch_all.return_value = [_full_row()]
    repo = ReportJobRepository(db_mock)

    items = asyncio.run(
        repo.list_jobs(
            status="pending", report_type="locker_open", limit=10, offset=20
        )
    )
    assert len(items) == 1

    args, _ = db_mock.fetch_all.call_args
    sql, params = args[0], args[1]
    assert "status = %s" in sql
    assert "report_type = %s" in sql
    assert "ORDER BY created_at DESC" in sql
    assert "LIMIT %s OFFSET %s" in sql
    assert params == ("pending", "locker_open", 10, 20)


def test_list_jobs_no_filters(db_mock):
    db_mock.fetch_all.return_value = []
    repo = ReportJobRepository(db_mock)

    asyncio.run(repo.list_jobs(status=None, report_type=None, limit=5, offset=0))
    args, _ = db_mock.fetch_all.call_args
    sql, params = args[0], args[1]
    assert "status = %s" not in sql
    assert "report_type = %s" not in sql
    assert params == (5, 0)


def test_list_jobs_filters_by_q(db_mock):
    db_mock.fetch_all.return_value = []
    repo = ReportJobRepository(db_mock)

    asyncio.run(
        repo.list_jobs(
            status=None, report_type=None, q="quarter", limit=10, offset=0
        )
    )

    args, _ = db_mock.fetch_all.call_args
    sql, params = args[0], args[1]
    assert "name ILIKE %s ESCAPE '\\'" in sql
    assert params == ("%quarter%", 10, 0)


def test_list_jobs_escapes_q_metachars(db_mock):
    db_mock.fetch_all.return_value = []
    repo = ReportJobRepository(db_mock)

    asyncio.run(
        repo.list_jobs(status=None, report_type=None, q="50%", limit=10, offset=0)
    )

    args, _ = db_mock.fetch_all.call_args
    params = args[1]
    assert params[0] == "%50\\%%"


def test_count_jobs_filters_by_q(db_mock):
    db_mock.fetch_one.return_value = {"count": 2}
    repo = ReportJobRepository(db_mock)

    total = asyncio.run(
        repo.count_jobs(status=None, report_type=None, q="quarter")
    )
    assert total == 2

    args, _ = db_mock.fetch_one.call_args
    sql, params = args[0], args[1]
    assert "name ILIKE %s ESCAPE '\\'" in sql
    assert "%quarter%" in params


def test_count_jobs_reads_count(db_mock):
    db_mock.fetch_one.return_value = {"count": 7}
    repo = ReportJobRepository(db_mock)

    total = asyncio.run(repo.count_jobs(status="pending", report_type=None))
    assert total == 7

    args, _ = db_mock.fetch_one.call_args
    sql, params = args[0], args[1]
    assert "count(*) AS count" in sql
    assert "status = %s" in sql
    assert params == ("pending",)


def test_count_jobs_zero_when_no_row(db_mock):
    db_mock.fetch_one.return_value = None
    repo = ReportJobRepository(db_mock)
    assert asyncio.run(repo.count_jobs(status=None, report_type=None)) == 0


# ---------------------------------------------------------------------------
# Worker / cleanup methods
# ---------------------------------------------------------------------------


def test_claim_pending_returns_model(db_mock):
    db_mock.fetch_one.return_value = _full_row(status="running")
    repo = ReportJobRepository(db_mock)

    job = asyncio.run(repo.claim_pending())

    assert job is not None
    assert job.status is ReportStatus.RUNNING
    args, kwargs = db_mock.fetch_one.call_args
    assert "FOR UPDATE SKIP LOCKED" in args[0]
    assert "status = 'running'" in args[0]
    assert kwargs.get("read_only") is False


def test_claim_pending_returns_none_when_empty(db_mock):
    db_mock.fetch_one.return_value = None
    repo = ReportJobRepository(db_mock)
    assert asyncio.run(repo.claim_pending()) is None


def test_claim_pending_reclaims_stale_running(db_mock):
    """claim_pending захватывает не только pending, но и зависшие running.

    Зависшая running-задача (worker упал после flip в running) реклеймится по
    lease-таймауту: SELECT должен матчить running старше lease (COR-13).
    """
    from app.reports_data.db.repositories.report_job import RUNNING_JOB_LEASE_SECONDS

    db_mock.fetch_one.return_value = _full_row(status="running", attempts=2)
    repo = ReportJobRepository(db_mock)

    job = asyncio.run(repo.claim_pending())

    assert job is not None
    args, _ = db_mock.fetch_one.call_args
    sql = args[0]
    # Реклейм по двум статусам в одном атомарном запросе.
    assert "status = 'pending'" in sql
    assert "status = 'running'" in sql
    # Lease выражен через started_at и интервал — свежий running не трогаем.
    assert "started_at" in sql
    assert str(RUNNING_JOB_LEASE_SECONDS) in sql
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_running_job_lease_is_generous():
    """Lease должен быть заметно больше типичного времени генерации (минуты)."""
    from app.reports_data.db.repositories.report_job import RUNNING_JOB_LEASE_SECONDS

    assert RUNNING_JOB_LEASE_SECONDS >= 300


def test_mark_ready_updates_all_fields(db_mock):
    repo = ReportJobRepository(db_mock)
    expires = datetime(2026, 6, 1, 0, 0, 0)

    asyncio.run(
        repo.mark_ready(
            "id-1",
            fmt="xlsx",
            file_path="/data/reports/id-1.xlsx",
            file_name="locker_open_20260526.xlsx",
            file_size=123,
            row_count=9,
            expires_at=expires,
        )
    )

    args, _ = db_mock.execute.call_args
    sql, params = args[0], args[1]
    assert "status = %s" in sql
    assert "completed_at = now()" in sql
    assert params == (
        ReportStatus.READY.value,
        "xlsx",
        "/data/reports/id-1.xlsx",
        "locker_open_20260526.xlsx",
        123,
        9,
        expires,
        "id-1",
    )


def test_mark_failed_truncates_message(db_mock):
    repo = ReportJobRepository(db_mock)
    long_msg = "x" * 5000

    asyncio.run(repo.mark_failed("id-1", long_msg))

    args, _ = db_mock.execute.call_args
    sql, params = args[0], args[1]
    assert "status = %s" in sql
    assert params[0] == ReportStatus.FAILED.value
    assert len(params[1]) == 1000
    assert params[2] == "id-1"


def test_requeue_sets_pending(db_mock):
    repo = ReportJobRepository(db_mock)
    asyncio.run(repo.requeue("id-1"))
    args, _ = db_mock.execute.call_args
    assert args[1] == (ReportStatus.PENDING.value, "id-1")


def test_mark_expired_sets_expired(db_mock):
    repo = ReportJobRepository(db_mock)
    asyncio.run(repo.mark_expired("id-1"))
    args, _ = db_mock.execute.call_args
    assert args[1] == (ReportStatus.EXPIRED.value, "id-1")


def test_list_expired_ready_filters(db_mock):
    db_mock.fetch_all.return_value = [_full_row(status="ready")]
    repo = ReportJobRepository(db_mock)

    items = asyncio.run(repo.list_expired_ready())

    assert len(items) == 1
    args, _ = db_mock.fetch_all.call_args
    assert "status = 'ready'" in args[0]
    assert "expires_at < now()" in args[0]


# ---------------------------------------------------------------------------
# Owner-scoping (SEC-06 / IDOR): requested_by-фильтр в SQL
# ---------------------------------------------------------------------------


def test_get_by_id_owner_scoped_adds_where(db_mock):
    db_mock.fetch_one.return_value = _full_row(requested_by=42)
    repo = ReportJobRepository(db_mock)

    asyncio.run(repo.get_by_id("id-1", requested_by=42))

    args, _ = db_mock.fetch_one.call_args
    sql, params = args[0], args[1]
    assert "requested_by = %s" in sql
    assert params == ("id-1", 42)


def test_get_by_id_admin_no_owner_filter(db_mock):
    db_mock.fetch_one.return_value = _full_row()
    repo = ReportJobRepository(db_mock)

    asyncio.run(repo.get_by_id("id-1", requested_by=None))

    args, _ = db_mock.fetch_one.call_args
    sql, params = args[0], args[1]
    assert "requested_by = %s" not in sql
    assert params == ("id-1",)


def test_update_name_owner_scoped_adds_where(db_mock):
    db_mock.fetch_one.return_value = _full_row(name="Renamed", requested_by=42)
    repo = ReportJobRepository(db_mock)

    asyncio.run(repo.update_name("id-1", "Renamed", requested_by=42))

    args, _ = db_mock.fetch_one.call_args
    sql, params = args[0], args[1]
    assert "requested_by = %s" in sql
    assert params == ("Renamed", "id-1", 42)


def test_update_name_admin_no_owner_filter(db_mock):
    db_mock.fetch_one.return_value = _full_row(name="Renamed")
    repo = ReportJobRepository(db_mock)

    asyncio.run(repo.update_name("id-1", "Renamed", requested_by=None))

    args, _ = db_mock.fetch_one.call_args
    sql, params = args[0], args[1]
    assert "requested_by = %s" not in sql
    assert params == ("Renamed", "id-1")


def test_list_jobs_owner_scoped(db_mock):
    db_mock.fetch_all.return_value = []
    repo = ReportJobRepository(db_mock)

    asyncio.run(
        repo.list_jobs(
            status=None, report_type=None, requested_by=42, limit=5, offset=0
        )
    )

    args, _ = db_mock.fetch_all.call_args
    sql, params = args[0], args[1]
    assert "requested_by = %s" in sql
    assert params == (42, 5, 0)


def test_count_jobs_owner_scoped(db_mock):
    db_mock.fetch_one.return_value = {"count": 1}
    repo = ReportJobRepository(db_mock)

    asyncio.run(
        repo.count_jobs(status=None, report_type=None, requested_by=42)
    )

    args, _ = db_mock.fetch_one.call_args
    sql, params = args[0], args[1]
    assert "requested_by = %s" in sql
    assert params == (42,)
