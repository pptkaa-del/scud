from pathlib import Path

from .pylightxl import Database, writexl
from .styled import write_styled_table


def write_table(out_path: Path, *, sheet_name: str, headers: list[str], rows: list[list[object]]) -> None:
    """Write a single-sheet .xlsx: header row (row 1) then data rows. 1-based indexing."""
    db = Database()
    db.add_ws(sheet_name)
    ws = db.ws(sheet_name)
    for col, header in enumerate(headers, start=1):
        ws.update_index(row=1, col=col, val=header)
    for r, record in enumerate(rows, start=2):
        for col, value in enumerate(record, start=1):
            ws.update_index(row=r, col=col, val="" if value is None else value)
    writexl(db, str(out_path))


__all__ = ["Database", "write_styled_table", "write_table", "writexl"]
