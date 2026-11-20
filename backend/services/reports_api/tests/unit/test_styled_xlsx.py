"""Юнит-тесты стилизованного xlsx-writer (shared.xlsx.write_styled_table).

Парсинг — только stdlib (zipfile + ElementTree): в test-стейдже reports_api нет
openpyxl, а тянуть его ради тестов запрещено правилами сертификации.
"""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

from shared.xlsx import write_styled_table

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_HEADERS = ["№ п/п", "ID пользователя", "Инв. номер шкафа", "ФИО", "Время открытия"]
_TITLE = "Отчёт по открытию шкафов · 01.05.2026–27.05.2026"
_ROWS: list[list[object]] = [
    [1, 2, "EMU-0B0E4652", "Оператор Тестовый 1", "2026-05-27 10:00:00"],
    [2, 3, "EMU-48F181AA", "Оператор Тестовый 2", "2026-05-27 10:01:00"],
    [3, 4, "EMU-5723F292", "Оператор Тестовый 3", "2026-05-27 10:02:00"],
]


def _write(tmp_path: Path, rows: list[list[object]]) -> Path:
    out = tmp_path / "report.xlsx"
    write_styled_table(
        out, sheet_name="Открытия", title=_TITLE, headers=_HEADERS, rows=rows
    )
    return out


def _write_custom(
    tmp_path: Path, *, title: str, headers: list[str], rows: list[list[object]]
) -> Path:
    out = tmp_path / "report.xlsx"
    write_styled_table(
        out, sheet_name="Открытия", title=title, headers=headers, rows=rows
    )
    return out


def _parts(out: Path) -> tuple[list[str], str, str, str]:
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
        shared = z.read("xl/sharedStrings.xml").decode("utf-8")
        styles = z.read("xl/styles.xml").decode("utf-8")
    return names, sheet, shared, styles


def _shared_strings(shared_xml: str) -> list[str]:
    root = ET.fromstring(shared_xml)
    out: list[str] = []
    for si in root.findall("m:si", _NS):
        t = si.find("m:t", _NS)
        out.append("" if t is None or t.text is None else t.text)
    return out


def _cell(sheet_root: ET.Element, shared: list[str], addr: str) -> object:
    for c in sheet_root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
        if c.get("r") == addr:
            v = c.find("m:v", _NS)
            if v is None or v.text is None:
                return None
            if c.get("t") == "s":
                return shared[int(v.text)]
            return v.text
    return None


def test_zip_contains_styles_and_shared_strings(tmp_path):
    names, _sheet, _shared, _styles = _parts(_write(tmp_path, _ROWS))
    assert "xl/styles.xml" in names
    assert "xl/worksheets/sheet1.xml" in names
    assert "xl/sharedStrings.xml" in names


def test_title_merged_across_all_columns(tmp_path):
    _names, sheet, shared, _styles = _parts(_write(tmp_path, _ROWS))
    root = ET.fromstring(sheet)
    assert _cell(root, _shared_strings(shared), "A1") == _TITLE
    assert 'ref="A1:E1"' in sheet


def test_header_on_row2_data_from_row3(tmp_path):
    _names, sheet, shared, _styles = _parts(_write(tmp_path, _ROWS))
    root = ET.fromstring(sheet)
    ss = _shared_strings(shared)
    header = [_cell(root, ss, f"{col}2") for col in ("A", "B", "C", "D", "E")]
    assert header == _HEADERS
    assert _cell(root, ss, "A3") == "1"
    assert _cell(root, ss, "B3") == "2"
    assert _cell(root, ss, "D3") == "Оператор Тестовый 1"
    assert _cell(root, ss, "C5") == "EMU-5723F292"


def test_numeric_cells_have_no_string_type(tmp_path):
    _names, sheet, _shared, _styles = _parts(_write(tmp_path, _ROWS))
    root = ET.fromstring(sheet)
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    a3 = next(c for c in root.iter(ns + "c") if c.get("r") == "A3")
    c3 = next(c for c in root.iter(ns + "c") if c.get("r") == "C3")
    assert a3.get("t") is None
    assert c3.get("t") == "s"


def test_frozen_header_and_autofilter_and_widths(tmp_path):
    _names, sheet, _shared, _styles = _parts(_write(tmp_path, _ROWS))
    assert 'ySplit="2"' in sheet
    assert 'state="frozen"' in sheet
    assert 'topLeftCell="A3"' in sheet
    assert 'autoFilter ref="A2:E2"' in sheet
    assert "customWidth" in sheet


def test_styles_carry_theme_colors(tmp_path):
    _names, _sheet, _shared, styles = _parts(_write(tmp_path, _ROWS))
    assert "FF1F4E78" in styles
    assert "FF2E75B6" in styles
    assert "FFD9E1F2" in styles
    assert 'style="thin"' in styles


def test_empty_period_keeps_title_and_header(tmp_path):
    _names, sheet, shared, _styles = _parts(_write(tmp_path, []))
    root = ET.fromstring(sheet)
    ss = _shared_strings(shared)
    assert _cell(root, ss, "A1") == _TITLE
    header = [_cell(root, ss, f"{col}2") for col in ("A", "B", "C", "D", "E")]
    assert header == _HEADERS


def test_formula_injection_triggers_are_guarded(tmp_path):
    """Строки с ведущим formula-триггером пишутся с защитным апострофом (SEC-16)."""
    # Хвост каждого payload не содержит XML-нормализуемых символов, поэтому
    # сравниваем по префиксу-апострофу + первому триггерному символу. Для \r
    # XML-парсер нормализует CR→LF при чтении (спецификация XML), поэтому точное
    # равенство всей строки тут не применимо — проверяем именно защитный префикс.
    payloads = [
        "=cmd|'/c calc'!A1",
        "+1+1",
        "-1+1",
        "@SUM(A1)",
        "\tTAB",
        "\rCR",
    ]
    rows: list[list[object]] = [
        [i + 1, 0, payload, payload, "ok"] for i, payload in enumerate(payloads)
    ]
    _names, sheet, shared, _styles = _parts(_write(tmp_path, rows))
    root = ET.fromstring(sheet)
    ss = _shared_strings(shared)
    for i, payload in enumerate(payloads):
        excel_row = i + 3
        for col in ("C", "D"):
            stored = _cell(root, ss, f"{col}{excel_row}")
            assert stored is not None
            assert stored.startswith("'"), (col, excel_row, stored)
            assert len(stored) == len(payload) + 1, (col, excel_row, stored)


def test_legitimate_string_values_are_not_guarded(tmp_path):
    """Обычные значения не получают апостроф; защищается только ведущий триггер."""
    rows: list[list[object]] = [
        [1, 2, "INV-001", "Иванов", "ok"],
        [2, 3, "EMU-0B0E4652", "O'Brien", "1=1 в середине"],
        [3, 4, "a=b", "контакт@домен", "тире-внутри"],
    ]
    _names, sheet, shared, _styles = _parts(_write(tmp_path, rows))
    root = ET.fromstring(sheet)
    ss = _shared_strings(shared)
    assert _cell(root, ss, "C3") == "INV-001"
    assert _cell(root, ss, "D3") == "Иванов"
    assert _cell(root, ss, "C4") == "EMU-0B0E4652"
    assert _cell(root, ss, "D4") == "O'Brien"
    assert _cell(root, ss, "E4") == "1=1 в середине"
    assert _cell(root, ss, "C5") == "a=b"
    assert _cell(root, ss, "D5") == "контакт@домен"
    assert _cell(root, ss, "E5") == "тире-внутри"


def test_numeric_cells_unaffected_by_guard(tmp_path):
    """Числовые ячейки не превращаются в строки и не получают апостроф."""
    rows: list[list[object]] = [[1, -5, "=danger", "x", "y"]]
    _names, sheet, _shared, _styles = _parts(_write(tmp_path, rows))
    root = ET.fromstring(sheet)
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    b3 = next(c for c in root.iter(ns + "c") if c.get("r") == "B3")
    assert b3.get("t") is None
    v = b3.find("m:v", _NS)
    assert v is not None
    assert v.text == "-5"


def test_title_formula_injection_is_guarded(tmp_path):
    """Заголовок с ведущим formula-триггером пишется с защитным апострофом (SEC-16)."""
    dangerous = "=cmd|'/c calc'!A1"
    out = _write_custom(tmp_path, title=dangerous, headers=_HEADERS, rows=_ROWS)
    _names, sheet, shared, _styles = _parts(out)
    root = ET.fromstring(sheet)
    ss = _shared_strings(shared)
    stored = _cell(root, ss, "A1")
    assert stored is not None
    assert stored == "'" + dangerous
    # Незащищённое опасное значение не должно появиться в shared strings.
    assert dangerous not in ss


def test_header_formula_injection_is_guarded(tmp_path):
    """Ячейка шапки с ведущим formula-триггером защищается апострофом (SEC-16)."""
    headers = ["№ п/п", "=2+2", "+danger", "-evil", "@SUM(A1)"]
    out = _write_custom(tmp_path, title=_TITLE, headers=headers, rows=[])
    _names, sheet, shared, _styles = _parts(out)
    root = ET.fromstring(sheet)
    ss = _shared_strings(shared)
    for col, raw in zip(("A", "B", "C", "D", "E"), headers, strict=False):
        stored = _cell(root, ss, f"{col}2")
        assert stored is not None
        expected = "'" + raw if raw.startswith(("=", "+", "-", "@")) else raw
        assert stored == expected, (col, stored)
    # Незащищённые опасные заголовки не должны появиться в shared strings.
    for raw in ("=2+2", "+danger", "-evil", "@SUM(A1)"):
        assert raw not in ss


def test_safe_title_and_headers_not_guarded(tmp_path):
    """Безопасные title/headers без триггера эмитятся без изменений (нет регресса)."""
    title = "Отчёт по открытию шкафов"
    headers = ["№ п/п", "ID пользователя", "Инв. номер шкафа", "ФИО", "Время"]
    out = _write_custom(tmp_path, title=title, headers=headers, rows=[])
    _names, sheet, shared, _styles = _parts(out)
    root = ET.fromstring(sheet)
    ss = _shared_strings(shared)
    assert _cell(root, ss, "A1") == title
    stored_headers = [_cell(root, ss, f"{col}2") for col in ("A", "B", "C", "D", "E")]
    assert stored_headers == headers
