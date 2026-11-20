"""Реестр генераторов отчётов — точка расширения.

Новый тип отчёта добавляется одной строкой в `generators` ниже; ключ словаря —
`report_type` генератора, по нему worker/handler выбирает реализацию.
"""

from __future__ import annotations

from app.reports.base import ReportGenerator
from app.reports.generators.locker_open import LockerOpenGenerator
from app.services.history_client import HistoryReportClient
from app.services.user_client import UserApiClient


def build_registry(
    history_client: HistoryReportClient, user_client: UserApiClient
) -> dict[str, ReportGenerator]:
    generators: list[ReportGenerator] = [
        LockerOpenGenerator(history_client, user_client)
    ]
    return {g.report_type: g for g in generators}
