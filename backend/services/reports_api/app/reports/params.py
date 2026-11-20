from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def reject_unknown_params(raw: Mapping[str, Any], allowed: Iterable[str]) -> None:
    """Отклонить произвольные ключи в params отчёта.

    Защита от инъекции произвольных данных в JSONB-колонку `report_jobs.params`:
    запрос отчёта не должен протаскивать ничего сверх объявленного генератором
    набора ключей. Вызывается генератором ПЕРВЫМ — до разбора значений — чтобы
    мусор отвергался при любом вводе. ``ValueError`` перехватывается handler'ом
    и превращается в 400 (произвольные данные в БД не попадают).
    """
    allowed_set = set(allowed)
    extra = sorted(key for key in raw if key not in allowed_set)
    if extra:
        raise ValueError(f"unknown params: {', '.join(extra)}")


@dataclass(slots=True)
class LockerOpenParams:
    date_from: datetime
    date_to: datetime
