"""Стилизованный .xlsx writer — чистый stdlib, без сторонних пакетов.

pylightxl пишет лист без оформления (нет styles.xml, ширин, закрепления,
объединений). Этот модуль собирает OOXML вручную через zipfile и добавляет
то оформление, которое требуется отчётам reports_api: объединённый заголовок,
цветную жирную шапку, тонкие рамки, «зебру», авто-ширину колонок, закреплённую
шапку и автофильтр. Совместим с сертификацией Astra — только stdlib.
"""

from __future__ import annotations

from pathlib import Path
import zipfile

# Палитра (ARGB) — тема «корпоративный синий».
_TITLE_FILL = "FF1F4E78"
_HEADER_FILL = "FF2E75B6"
_ZEBRA_FILL = "FFD9E1F2"
_WHITE = "FFFFFFFF"
_BORDER = "FFBFBFBF"

# Индексы стилей в cellXfs (порядок задаётся _STYLES).
_S_TITLE = 1
_S_HEADER = 2
_S_TEXT = 3
_S_TEXT_ZEBRA = 4
_S_NUM = 5
_S_NUM_ZEBRA = 6

# Авто-ширина колонок (в «символах» Excel).
_COL_MIN = 6.0
_COL_MAX = 60.0
_COL_PAD = 2.0
_COL_FACTOR = 1.1
_TITLE_ROW_HEIGHT = 24.0

# Защита от formula/DDE-инъекции (OWASP CSV/formula injection). Если строковая
# ячейка начинается с одного из этих символов, spreadsheet-приложение (Excel /
# LibreOffice) трактует значение как формулу. Префиксуем безопасным апострофом,
# чтобы значение осталось буквальным текстом. Касается ТОЛЬКО строковых ячеек и
# ТОЛЬКО ведущего символа — середина строки ("a=b", "O'Brien") не затрагивается.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_FORMULA_GUARD = "'"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    "</Types>"
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)

_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
    "</Relationships>"
)

_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets>'
    "</workbook>"
)

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="3">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    f'<font><b/><sz val="14"/><color rgb="{_WHITE}"/><name val="Calibri"/></font>'
    f'<font><b/><sz val="11"/><color rgb="{_WHITE}"/><name val="Calibri"/></font>'
    "</fonts>"
    '<fills count="5">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    f'<fill><patternFill patternType="solid"><fgColor rgb="{_TITLE_FILL}"/></patternFill></fill>'
    f'<fill><patternFill patternType="solid"><fgColor rgb="{_HEADER_FILL}"/></patternFill></fill>'
    f'<fill><patternFill patternType="solid"><fgColor rgb="{_ZEBRA_FILL}"/></patternFill></fill>'
    "</fills>"
    '<borders count="2">'
    "<border><left/><right/><top/><bottom/><diagonal/></border>"
    f'<border><left style="thin"><color rgb="{_BORDER}"/></left>'
    f'<right style="thin"><color rgb="{_BORDER}"/></right>'
    f'<top style="thin"><color rgb="{_BORDER}"/></top>'
    f'<bottom style="thin"><color rgb="{_BORDER}"/></bottom><diagonal/></border>'
    "</borders>"
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="7">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _guard_formula(text: str) -> str:
    """Нейтрализовать formula/DDE-инъекцию в строковой ячейке (SEC-16).

    Если значение начинается с триггера формулы, префиксует апострофом, чтобы
    spreadsheet-приложение трактовало его как текст. Иначе возвращает без
    изменений. Защищается только ведущий символ.
    """
    if text.startswith(_FORMULA_TRIGGERS):
        return _FORMULA_GUARD + text
    return text


def _col_letter(col: int) -> str:
    """1-based индекс колонки → буквенный адрес (1→A, 27→AA)."""
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def write_styled_table(
    out_path: Path,
    *,
    sheet_name: str,
    title: str,
    headers: list[str],
    rows: list[list[object]],
    column_widths: list[float] | None = None,
) -> None:
    """Записать однолистовой стилизованный .xlsx.

    Строка 1 — объединённый заголовок (title), строка 2 — шапка (headers),
    строки 3+ — данные (rows). Числовые ячейки пишутся числами, остальные —
    через sharedStrings. Шапка закрепляется, на неё ставится автофильтр.
    """
    ncols = len(headers)
    if ncols == 0:
        raise ValueError("headers must not be empty")
    if column_widths is not None and len(column_widths) != ncols:
        raise ValueError("column_widths length must match headers")

    shared: dict[str, int] = {}
    ref_count = 0

    def intern(text: str) -> int:
        nonlocal ref_count
        ref_count += 1
        idx = shared.get(text)
        if idx is None:
            idx = len(shared)
            shared[text] = idx
        return idx

    last_col = _col_letter(ncols)
    last_row = 2 + len(rows)

    if column_widths is not None:
        widths = list(column_widths)
    else:
        widths = []
        for c in range(ncols):
            longest = len(headers[c])
            for row in rows:
                if c < len(row):
                    cell = row[c]
                    longest = max(longest, len("" if cell is None else str(cell)))
            widths.append(min(_COL_MAX, max(_COL_MIN, longest * _COL_FACTOR + _COL_PAD)))

    cols_xml = "".join(
        f'<col min="{c + 1}" max="{c + 1}" width="{widths[c]:.2f}" customWidth="1"/>'
        for c in range(ncols)
    )

    # Строка 1: заголовок. Якорь A1 со значением + пустые styled-ячейки на остаток
    # объединения, чтобы заливка покрыла всю ширину (стандартный приём, как у
    # xlsxwriter: пустые форматированные ячейки внутри merge не вызывают repair).
    title_cells = f'<c r="A1" s="{_S_TITLE}" t="s"><v>{intern(_guard_formula(str(title)))}</v></c>'
    title_cells += "".join(
        f'<c r="{_col_letter(c + 1)}1" s="{_S_TITLE}"/>' for c in range(1, ncols)
    )
    rows_xml = [f'<row r="1" ht="{_TITLE_ROW_HEIGHT}" customHeight="1">{title_cells}</row>']

    header_cells = "".join(
        f'<c r="{_col_letter(c + 1)}2" s="{_S_HEADER}" t="s"><v>{intern(_guard_formula(str(headers[c])))}</v></c>'
        for c in range(ncols)
    )
    rows_xml.append(f'<row r="2">{header_cells}</row>')

    for r, row in enumerate(rows):
        excel_row = r + 3
        zebra = r % 2 == 1
        cells = ""
        for c in range(ncols):
            addr = f"{_col_letter(c + 1)}{excel_row}"
            value = row[c] if c < len(row) else None
            if _is_number(value):
                style = _S_NUM_ZEBRA if zebra else _S_NUM
                cells += f'<c r="{addr}" s="{style}"><v>{value}</v></c>'
            else:
                text = "" if value is None else _guard_formula(str(value))
                style = _S_TEXT_ZEBRA if zebra else _S_TEXT
                if text == "":
                    cells += f'<c r="{addr}" s="{style}"/>'
                else:
                    cells += f'<c r="{addr}" s="{style}" t="s"><v>{intern(text)}</v></c>'
        rows_xml.append(f'<row r="{excel_row}">{cells}</row>')

    worksheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="A1:{last_col}{last_row}"/>'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A3" sqref="A3"/>'
        "</sheetView></sheetViews>"
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"<cols>{cols_xml}</cols>"
        f"<sheetData>{''.join(rows_xml)}</sheetData>"
        f'<autoFilter ref="A2:{last_col}2"/>'
        f'<mergeCells count="1"><mergeCell ref="A1:{last_col}1"/></mergeCells>'
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        "</worksheet>"
    )

    ordered = sorted(shared.items(), key=lambda kv: kv[1])
    si = "".join(f'<si><t xml:space="preserve">{_escape(s)}</t></si>' for s, _ in ordered)
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{ref_count}" uniqueCount="{len(shared)}">{si}</sst>'
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK.format(sheet_name=_escape(sheet_name)))
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/sharedStrings.xml", shared_xml)
        z.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
