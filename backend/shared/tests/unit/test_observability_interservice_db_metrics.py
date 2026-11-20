"""Unit-тесты Фазы B4: контракт enum InterserviceOutcome и двух новых
счётчиков (scud_interservice_requests_total, scud_db_errors_total).

Тесты на сам инкремент из retry_client / record_db_error_metric — в
test_retry_client_interservice_metric.py (этот файл) и в locker_api-сьюте
(DB-часть требует psycopg2, которого нет в shared_tests-образе)."""
from __future__ import annotations

from shared.enums import InterserviceOutcome


def test_interservice_outcome_values() -> None:
    assert InterserviceOutcome.SUCCESS.value == "success"
    assert InterserviceOutcome.RETRY.value == "retry"
    assert InterserviceOutcome.ERROR.value == "error"
    assert InterserviceOutcome.EXHAUSTED.value == "exhausted"


def test_interservice_counter_registered_with_labels() -> None:
    from shared.metrics import interservice_requests_total
    # Не кидает на полном наборе меток — контракт label-набора.
    interservice_requests_total.labels(
        service="user_api",
        target="acm",
        method="GET",
        outcome=InterserviceOutcome.SUCCESS.value,
    ).inc()


def test_db_errors_counter_registered_with_labels() -> None:
    from shared.metrics import db_errors_total
    db_errors_total.labels(
        service="locker_api",
        operation="execute",
        error_code="40001",
    ).inc()
