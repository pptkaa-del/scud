from pathlib import Path
import zipfile

from shared.xlsx import write_table


def test_write_table_produces_valid_xlsx(tmp_path: Path):
    out = tmp_path / "r.xlsx"
    write_table(out, sheet_name="Открытия",
                headers=["№ п/п", "ФИО"], rows=[[1, "Иванов Иван"], [2, None]])
    assert out.exists()
    assert out.stat().st_size > 0
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "xl/workbook.xml" in names
        assert any("sheet" in n for n in names)
        blob = b"".join(z.read(n) for n in names if n.endswith(".xml"))
    assert "Иванов Иван".encode() in blob
