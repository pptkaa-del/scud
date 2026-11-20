"""Генератор отчёта `locker_open`: события открытия шкафов → .xlsx.

Тянет страницы событий из history_api через `HistoryReportClient`, резолвит
ФИО по `created_by` через `UserApiClient` (поле `user_full_name` в
command_history никогда не заполняется locker_api), нумерует строки и пишет
один лист .xlsx через `shared.xlsx.write_styled_table`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.reports.base import GenerationResult
from app.reports.formats import ReportFormat
from app.reports.params import LockerOpenParams, reject_unknown_params
from app.services.history_client import HistoryReportClient, OpenEvent
from app.services.user_client import UserApiClient
from shared.xlsx import write_styled_table

_PAGE = 1000
# Ровно те ключи, что генератор читает из params; всё прочее — отклоняется.
_ALLOWED_PARAMS = frozenset({"date_from", "date_to"})

# COR-03 (OOM/DoS): жёсткие ограничения объёма отчёта.
#
# MAX_REPORT_WINDOW_DAYS — максимальная ширина окна дат. Проверяется в
# validate_params, поэтому слишком широкий запрос отвергается синхронным 400
# уже на этапе постановки задачи (handler ReportListHandler.post), а не падает
# в воркере. 92 дня ≈ квартал — практический верх для оперативной отчётности.
MAX_REPORT_WINDOW_DAYS = 92

# MAX_REPORT_ROWS — потолок числа строк, при превышении которого generate()
# прекращает выборку и падает с понятной ошибкой (job → failed). Нужен как
# вторая линия обороны: даже в пределах окна патологический шкаф/набор событий
# не должен раздувать память.
#
# Обоснование величины относительно mem_limit=1g контейнера. write_styled_table
# строит весь worksheet XML одной Python-строкой (см. shared/xlsx/styled.py):
#   - на строку ~5 ячеек × ~80 байт XML ≈ 400 Б; список rows_xml + финальный
#     join ≈ удвоение → ~80 Б/строку × 2;
#   - сам worksheet_xml целиком ≈ ещё ~40 МБ при 100k строк;
#   - Python-списки events (dataclass) + rows (list-of-list) ≈ ~600 Б/строку.
# Пиковый транзиент при 100 000 строк ≈ 250–350 МБ — с запасом влезает в 1g
# вместе с интерпретатором и параллельной работой. 200k уже даёт ~0.5–0.7 ГБ
# (рискованно), поэтому потолок зафиксирован на 100k.
MAX_REPORT_ROWS = 100_000
_SHEET_NAME = "Открытия"
_HEADERS = ["№ п/п", "ID пользователя", "Инв. номер шкафа", "ФИО", "Время открытия"]
_TITLE_PREFIX = "Отчёт по открытию шкафов"


class LockerOpenGenerator:
    """Генератор отчёта об открытиях шкафов."""

    report_type = "locker_open"
    output_format = ReportFormat.XLSX

    def __init__(
        self, history_client: HistoryReportClient, user_client: UserApiClient
    ) -> None:
        self._history = history_client
        self._user = user_client

    def validate_params(self, raw: dict[str, Any]) -> LockerOpenParams:
        reject_unknown_params(raw, _ALLOWED_PARAMS)
        try:
            date_from = datetime.fromisoformat(raw["date_from"])
            date_to = datetime.fromisoformat(raw["date_to"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "date_from and date_to are required ISO-8601 datetimes"
            ) from exc
        if date_from >= date_to:
            raise ValueError("date_from must be before date_to")
        if (date_to - date_from) > timedelta(days=MAX_REPORT_WINDOW_DAYS):
            raise ValueError(
                f"диапазон дат слишком большой (максимум "
                f"{MAX_REPORT_WINDOW_DAYS} дней), сузьте даты"
            )
        return LockerOpenParams(date_from=date_from, date_to=date_to)

    def serialize_params(self, params: object) -> dict[str, Any]:
        if not isinstance(params, LockerOpenParams):
            raise TypeError(
                "LockerOpenGenerator.serialize_params requires LockerOpenParams"
            )
        # Канонический ISO из разобранных datetime — в БД не попадает сырая
        # строка клиента, только нормализованное значение фиксированной формы.
        return {
            "date_from": params.date_from.isoformat(),
            "date_to": params.date_to.isoformat(),
        }

    async def generate(self, params: object, out_path: Path) -> GenerationResult:
        if not isinstance(params, LockerOpenParams):
            raise TypeError("LockerOpenGenerator.generate requires LockerOpenParams")
        # 1. Пробуферить все события (нужны все created_by, прежде чем резолвить ФИО).
        events: list[OpenEvent] = []
        offset = 0
        while True:
            batch = await self._history.fetch_open_events(
                params.date_from, params.date_to, _PAGE, offset
            )
            events.extend(batch)
            # Жёсткий потолок строк (COR-03): прекращаем выборку и валим job,
            # как только накопленное превышает MAX_REPORT_ROWS, чтобы даже в
            # пределах окна не раздувать память на сборке xlsx.
            if len(events) > MAX_REPORT_ROWS:
                raise ValueError(
                    f"отчёт содержит слишком много строк (более "
                    f"{MAX_REPORT_ROWS}), сузьте диапазон дат"
                )
            if len(batch) < _PAGE:
                break
            offset += _PAGE
        # 2. Резолвить ФИО по уникальным created_by через user_api.
        distinct_ids = list({ev.created_by for ev in events if ev.created_by is not None})
        names = await self._user.fetch_names(distinct_ids)
        # 3. Собрать строки. ФИО — из резолва; fallback на user_full_name (на
        #    практике пуст, но безвреден), иначе пустая строка.
        rows: list[list[object]] = []
        for idx, ev in enumerate(events, start=1):
            resolved = names.get(ev.created_by) if ev.created_by is not None else None
            full_name = resolved or (ev.user_full_name or "")
            rows.append(
                [
                    idx,
                    ev.created_by,
                    ev.locker_inventory_number,
                    full_name,
                    ev.executed_at.isoformat(sep=" ", timespec="seconds"),
                ]
            )
        title = (
            f"{_TITLE_PREFIX} · "
            f"{params.date_from:%d.%m.%Y}–{params.date_to:%d.%m.%Y}"
        )
        write_styled_table(
            out_path, sheet_name=_SHEET_NAME, title=title, headers=_HEADERS, rows=rows
        )
        return GenerationResult(row_count=len(events))
