from __future__ import annotations

import pytest

from MCUM.core.spreadsheet_extractor import extract_workbook


openpyxl = pytest.importorskip("openpyxl")


def test_extract_workbook_returns_headers_samples_and_formula(tmp_path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["ID", "Name", "Amount"])
    sheet.append([1, "Alpha", 10])
    sheet.append([2, "Beta", 20])
    sheet["D1"] = "Total"
    sheet["D2"] = "=SUM(C2:C3)"
    source_path = tmp_path / "sample.xlsx"
    workbook.save(source_path)

    result = extract_workbook(source_path, max_sheets=5, max_rows=5, max_cols=5, max_scan_rows=20)

    assert result["status"] == "success"
    assert result["file_name"] == "sample.xlsx"
    assert result["workbook"]["sheet_count"] == 1
    assert result["sheets"][0]["name"] == "Data"
    assert result["sheets"][0]["detected_header_row"] == 1
    assert result["sheets"][0]["headers"][:3] == ["ID", "Name", "Amount"]
    assert result["sheets"][0]["sample_rows"][0]["values"][:3] == [1, "Alpha", 10]
    assert result["sheets"][0]["formula_cells_in_scan"][0]["cell"] == "D2"
