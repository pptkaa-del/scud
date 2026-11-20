import asyncio
from datetime import datetime, timedelta
import io
from pathlib import Path
from unittest.mock import AsyncMock
import zipfile

import pytest

from app.reports.generators.locker_open import (
    MAX_REPORT_ROWS,
    MAX_REPORT_WINDOW_DAYS,
    LockerOpenGenerator,
)
from app.services.history_client import OpenEvent


def _gen(events, names=None):
    history = AsyncMock()
    history.fetch_open_events = AsyncMock(side_effect=[events, []])
    user = AsyncMock()
    user.fetch_names = AsyncMock(return_value=names if names is not None else {})
    return LockerOpenGenerator(history, user)


def _xlsx_text(path: Path) -> str:
    """Конкатенировать весь XML из .xlsx для подстрочного поиска значений ячеек."""
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as zf:
        for name in zf.namelist():
            if name.endswith(".xml"):
                parts.append(zf.read(name).decode("utf-8", errors="ignore"))
    return "\n".join(parts)


def test_validate_params_ok():
    p = _gen([]).validate_params(
        {"date_from": "2026-01-01T00:00:00", "date_to": "2026-02-01T00:00:00"}
    )
    assert p.date_from < p.date_to


def test_validate_params_at_max_window_ok():
    """Окно ровно MAX_REPORT_WINDOW_DAYS — допустимо (граница включительно)."""
    start = datetime(2026, 1, 1, 0, 0, 0)
    end = start + timedelta(days=MAX_REPORT_WINDOW_DAYS)
    p = _gen([]).validate_params(
        {"date_from": start.isoformat(), "date_to": end.isoformat()}
    )
    assert (p.date_to - p.date_from).days == MAX_REPORT_WINDOW_DAYS


def test_validate_params_window_too_large_raises():
    """Окно больше MAX_REPORT_WINDOW_DAYS → 400 (диапазон слишком большой)."""
    start = datetime(2026, 1, 1, 0, 0, 0)
    end = start + timedelta(days=MAX_REPORT_WINDOW_DAYS, seconds=1)
    with pytest.raises(ValueError, match="слишком больш"):
        _gen([]).validate_params(
            {"date_from": start.isoformat(), "date_to": end.isoformat()}
        )


def test_generate_exceeding_row_ceiling_fails(tmp_path: Path):
    """Патологический locker: число строк превышает MAX_REPORT_ROWS →
    генерация падает с понятной ошибкой (память не раздувается)."""
    history = AsyncMock()
    # Каждая страница «полная» (_PAGE), поэтому fetch продолжается, пока не
    # превысит потолок. Возвращаем полные страницы бесконечно.
    full_page = [
        OpenEvent(1, "INV-1", "", datetime(2026, 1, 5, 10, 0)) for _ in range(1000)
    ]
    history.fetch_open_events = AsyncMock(return_value=full_page)
    user = AsyncMock()
    user.fetch_names = AsyncMock(return_value={})
    gen = LockerOpenGenerator(history, user)
    params = gen.validate_params(
        {"date_from": "2026-01-01T00:00:00", "date_to": "2026-02-01T00:00:00"}
    )
    with pytest.raises(ValueError, match="слишком много строк"):
        asyncio.run(gen.generate(params, tmp_path / "r.xlsx"))
    assert not (tmp_path / "r.xlsx").exists()


def test_validate_params_bad_order_raises():
    with pytest.raises(ValueError):
        _gen([]).validate_params(
            {"date_from": "2026-02-01T00:00:00", "date_to": "2026-01-01T00:00:00"}
        )


def test_validate_params_missing_raises():
    with pytest.raises(ValueError):
        _gen([]).validate_params({})


def test_validate_params_rejects_unknown_keys():
    """Произвольные данные в params отвергаются (не персистятся в БД)."""
    with pytest.raises(ValueError, match="evil"):
        _gen([]).validate_params(
            {
                "date_from": "2026-01-01T00:00:00",
                "date_to": "2026-02-01T00:00:00",
                "evil": "arbitrary",
            }
        )


def test_validate_params_rejects_unknown_keys_before_value_parse():
    """Неизвестный ключ отвергается, даже если обязательные ключи отсутствуют
    или некорректны — нельзя протащить произвольные данные ни при каком вводе."""
    with pytest.raises(ValueError, match="junk"):
        _gen([]).validate_params({"junk": {"$ne": None}})


def test_validate_params_unknown_key_wins_over_missing_required():
    """Один валидный ключ + один пропущен + неизвестный → ошибка про
    неизвестный ключ (атакующий не может замаскировать мусор пропуском поля)."""
    with pytest.raises(ValueError, match="evil"):
        _gen([]).validate_params(
            {"date_from": "2026-01-01T00:00:00", "evil": "x"}
        )


def test_serialize_params_returns_canonical_iso():
    """В БД пишется канонический ISO-вид из разобранного datetime, а не сырая
    строка клиента — произвольные значения клиента не персистятся дословно."""
    gen = _gen([])
    params = gen.validate_params(
        {"date_from": "2026-01-01T00:00:00", "date_to": "2026-02-01T00:00:00"}
    )
    stored = gen.serialize_params(params)
    assert stored == {
        "date_from": "2026-01-01T00:00:00",
        "date_to": "2026-02-01T00:00:00",
    }


def test_generate_writes_numbered_rows(tmp_path: Path):
    evs = [
        OpenEvent(7, "INV-1", "", datetime(2026, 1, 5, 10, 0)),
        OpenEvent(8, "INV-2", "", datetime(2026, 1, 6, 11, 0)),
    ]
    gen = _gen(evs, names={7: "Иванов И.И.", 8: "Петров П.П."})
    params = gen.validate_params(
        {"date_from": "2026-01-01T00:00:00", "date_to": "2026-02-01T00:00:00"}
    )
    result = asyncio.run(gen.generate(params, tmp_path / "r.xlsx"))
    assert result.row_count == 2
    assert (tmp_path / "r.xlsx").exists()


def test_generate_resolves_names_from_user_api(tmp_path: Path):
    """Ядро фикса: ФИО берётся из user_api по created_by, а не из пустого
    user_full_name. Проверяем, что резолв вызван с distinct created_by и
    что резолвленные ФИО присутствуют в xlsx."""
    evs = [
        OpenEvent(7, "INV-1", "", datetime(2026, 1, 5, 10, 0)),
        OpenEvent(8, "INV-2", "", datetime(2026, 1, 6, 11, 0)),
        # повтор created_by=7 → distinct сворачивает до {7, 8}.
        OpenEvent(7, "INV-3", "", datetime(2026, 1, 7, 12, 0)),
    ]
    gen = _gen(evs, names={7: "Иванов И.И.", 8: "Петров П.П."})
    params = gen.validate_params(
        {"date_from": "2026-01-01T00:00:00", "date_to": "2026-02-01T00:00:00"}
    )
    out = tmp_path / "r.xlsx"
    result = asyncio.run(gen.generate(params, out))

    assert result.row_count == 3
    # fetch_names вызван ровно одним набором distinct created_by.
    gen._user.fetch_names.assert_awaited_once()
    (called_ids,) = gen._user.fetch_names.await_args.args
    assert sorted(called_ids) == [7, 8]

    text = _xlsx_text(out)
    assert "Иванов И.И." in text
    assert "Петров П.П." in text


def test_generate_unknown_user_empty_name(tmp_path: Path):
    """Если user_api не вернул имя для created_by — ФИО пустое, без падения."""
    evs = [OpenEvent(7, "INV-1", "", datetime(2026, 1, 5, 10, 0))]
    gen = _gen(evs, names={})  # никого не нашли
    params = gen.validate_params(
        {"date_from": "2026-01-01T00:00:00", "date_to": "2026-02-01T00:00:00"}
    )
    out = tmp_path / "r.xlsx"
    result = asyncio.run(gen.generate(params, out))
    assert result.row_count == 1
    assert out.exists()


def test_generate_empty_period(tmp_path: Path):
    history = AsyncMock()
    history.fetch_open_events = AsyncMock(return_value=[])
    user = AsyncMock()
    user.fetch_names = AsyncMock(return_value={})
    gen = LockerOpenGenerator(history, user)
    params = gen.validate_params(
        {"date_from": "2026-01-01T00:00:00", "date_to": "2026-02-01T00:00:00"}
    )
    assert asyncio.run(gen.generate(params, tmp_path / "r.xlsx")).row_count == 0
