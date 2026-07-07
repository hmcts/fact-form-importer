import pytest
from zipfile import ZipFile

from fact_form_importer.ingest.workbook_profiler import (
    _column_index_from_cell_ref,
    excel_column_letter,
    profile_workbook,
)


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (0, "A"),
        (1, "B"),
        (25, "Z"),
        (26, "AA"),
        (27, "AB"),
        (51, "AZ"),
        (52, "BA"),
        (701, "ZZ"),
        (702, "AAA"),
    ],
)
def test_excel_column_letter(index, expected):
    assert excel_column_letter(index) == expected


def test_excel_column_letter_rejects_negative_index():
    with pytest.raises(ValueError, match="zero or greater"):
        excel_column_letter(-1)


def test_profile_csv_counts_empty_like_values_and_samples(tmp_path):
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Court,Email,Note",
                "Alpha Court,alpha@example.test,N/A",
                "Beta Court, ,Useful note",
                "Gamma Court,-,.",
            ]
        ),
        encoding="utf-8",
    )

    profile = profile_workbook(csv_path)

    assert profile.source_path == csv_path
    assert profile.sheet_name is None
    assert profile.row_count == 3
    assert profile.column_count == 3

    assert profile.columns[0].excel_letter == "A"
    assert profile.columns[0].header == "Court"
    assert profile.columns[0].non_empty_count == 3
    assert profile.columns[0].empty_count == 0
    assert profile.columns[0].sample_values == ["Alpha Court", "Beta Court", "Gamma Court"]

    assert profile.columns[1].header == "Email"
    assert profile.columns[1].non_empty_count == 1
    assert profile.columns[1].empty_count == 2
    assert profile.columns[1].sample_values == ["alpha@example.test"]

    assert profile.columns[2].header == "Note"
    assert profile.columns[2].non_empty_count == 1
    assert profile.columns[2].empty_count == 2
    assert profile.columns[2].sample_values == ["Useful note"]


def test_profile_empty_csv(tmp_path):
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("", encoding="utf-8")

    profile = profile_workbook(csv_path)

    assert profile.row_count == 0
    assert profile.column_count == 0
    assert profile.columns == []


def test_profile_rejects_unsupported_file_type(tmp_path):
    text_path = tmp_path / "sample.txt"
    text_path.write_text("hello", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported workbook type"):
        profile_workbook(text_path)


def test_column_index_from_cell_ref_rejects_invalid_reference():
    with pytest.raises(ValueError, match="Invalid cell reference"):
        _column_index_from_cell_ref("123")


def test_profile_xlsx_ignores_incorrect_dimension_metadata(tmp_path):
    xlsx_path = tmp_path / "bad-dimension.xlsx"
    _write_minimal_xlsx_with_bad_dimension(xlsx_path)

    profile = profile_workbook(xlsx_path)

    assert profile.sheet_name == "Sheet1"
    assert profile.row_count == 2
    assert profile.column_count == 3
    assert [column.excel_letter for column in profile.columns] == ["A", "B", "C"]
    assert [column.header for column in profile.columns] == ["ID", "Name", "Status"]
    assert profile.columns[2].sample_values == ["Open"]


def _write_minimal_xlsx_with_bad_dimension(path):
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets>
                <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
              </sheets>
            </workbook>
            """,
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1"
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
                Target="worksheets/sheet1.xml"/>
            </Relationships>
            """,
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <si><t>ID</t></si>
              <si><t>Name</t></si>
              <si><t>Status</t></si>
              <si><t>1</t></si>
              <si><t>Alpha Court</t></si>
              <si><t>Open</t></si>
            </sst>
            """,
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <dimension ref="A1"/>
              <sheetData>
                <row r="1">
                  <c r="A1" t="s"><v>0</v></c>
                  <c r="B1" t="s"><v>1</v></c>
                  <c r="C1" t="s"><v>2</v></c>
                </row>
                <row r="2">
                  <c r="A2" t="s"><v>3</v></c>
                  <c r="B2" t="s"><v>4</v></c>
                  <c r="C2" t="s"><v>5</v></c>
                </row>
                <row r="3">
                  <c r="A3"><v>2</v></c>
                </row>
              </sheetData>
            </worksheet>
            """,
        )
