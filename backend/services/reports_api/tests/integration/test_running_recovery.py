"""Интеграционные тесты восстановления зависших running-задач (COR-13).

Если worker упал/был убит (OOM) после flip задачи в `running`, но до
`mark_ready`/`requeue`/`mark_failed`, задача без реклейма зависала бы в `running`
навсегда: ничего её не возвращало в очередь, а бюджет попыток уже потрачен.

claim_pending реклеймит running старше lease (RUNNING_JOB_LEASE_SECONDS):
- зависшая running в пределах бюджета попыток — перегенерируется → ready;
- зависшая running сверх бюджета — терминально падает → failed (не зацикливается).
Свежая running (started_at в пределах lease) НЕ реклеймится — нет двойной
обработки задачи «в полёте».
"""

from __future__ import annotations

import time

from .conftest import insert_stuck_running_job

_DATE_FROM = "2026-09-10T00:00:00+00:00"
_DATE_TO = "2026-09-10T23:59:59+00:00"
_POLL_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.5

# Заведомо больше RUNNING_JOB_LEASE_SECONDS (600s по умолчанию).
_STALE_AGE_S = 3600


def _poll_status(api_client, report_id: str, *, want: set[str]) -> str:
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    last = None
    while time.monotonic() < deadline:
        resp = api_client.get(f"/api/v1/report/{report_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()["status"]
        if last in want:
            return last
        time.sleep(_POLL_INTERVAL_S)
    return last or "<none>"


def test_stale_running_within_budget_is_reclaimed_to_ready(api_client):
    """Зависшая running с устаревшим started_at реклеймится и доходит до ready."""
    report_id = insert_stuck_running_job(
        report_type="locker_open",
        params={"date_from": _DATE_FROM, "date_to": _DATE_TO},
        requested_by=1,
        attempts=1,
        started_age_seconds=_STALE_AGE_S,
    )

    final = _poll_status(api_client, report_id, want={"ready", "failed"})
    assert final == "ready", f"stale running not recovered (last={final})"

    # Очистка.
    api_client.delete(f"/api/v1/report/{report_id}")


def test_stale_running_over_budget_parks_at_failed(api_client):
    """Зависшая running сверх бюджета попыток → failed, без бесконечного цикла.

    attempts уже = max_attempts; claim инкрементит до max+1 → терминальный failed.
    """
    report_id = insert_stuck_running_job(
        report_type="locker_open",
        params={"date_from": _DATE_FROM, "date_to": _DATE_TO},
        requested_by=1,
        # REPORT_MAX_ATTEMPTS по умолчанию 3; claim сделает attempts=4 → сверх бюджета.
        attempts=3,
        started_age_seconds=_STALE_AGE_S,
    )

    final = _poll_status(api_client, report_id, want={"failed"})
    assert final == "failed", f"exhausted stale running not failed (last={final})"


def test_fresh_running_is_not_reclaimed(api_client):
    """Свежая running (в пределах lease) НЕ реклеймится — статус остаётся running."""
    report_id = insert_stuck_running_job(
        report_type="locker_open",
        params={"date_from": _DATE_FROM, "date_to": _DATE_TO},
        requested_by=1,
        attempts=1,
        # Свежая: started_at только что (в пределах lease).
        started_age_seconds=0,
    )

    # За окно поллинга статус не должен уйти из running (нет двойной обработки).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        resp = api_client.get(f"/api/v1/report/{report_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "running", "fresh running was reclaimed"
        time.sleep(_POLL_INTERVAL_S)
