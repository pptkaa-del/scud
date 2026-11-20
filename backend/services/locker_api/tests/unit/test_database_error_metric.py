"""Unit-тест хелпера record_db_error_metric (shared.database.retry).

Живёт в locker_api-сьюте: shared.database.retry импортирует psycopg2 на
уровне модуля, а shared_tests-образ его не несёт.

Проверяем: хелпер никогда не кидает и инкрементирует scud_db_errors_total
с переданным operation/error_code (через приватный _value prometheus)."""
from __future__ import annotations

from shared.database import retry


def test_record_db_error_metric_never_raises() -> None:
    retry.record_db_error_metric("execute", "40001")
    retry.record_db_error_metric("fetch_one", "pool_exhausted")
    retry.record_db_error_metric("fetch_all", "unknown")


def test_record_db_error_metric_increments_counter() -> None:
    assert retry._METRICS_AVAILABLE, "prometheus_client must be present in locker_api test image"
    from shared.metrics import SERVICE_NAME, db_errors_total
    c = db_errors_total.labels(service=SERVICE_NAME, operation="execute", error_code="40001")
    before = c._value.get()
    retry.record_db_error_metric("execute", "40001")
    assert c._value.get() == before + 1
