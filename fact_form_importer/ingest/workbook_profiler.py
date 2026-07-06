"""Profile spreadsheet shape and column completeness."""

from __future__ import annotations

import csv
import json
import math
import posixpath
import re
from zipfile import ZipFile
from pathlib import Path
from xml.etree import ElementTree
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

EMPTY_LIKE_STRINGS = {"", "n/a", "na", "-", "."}
MAX_SAMPLE_VALUES = 10
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CELL_REF_PATTERN = re.compile(r"^([A-Z]+)")


class ColumnProfile(BaseModel):
    index: int
    excel_letter: str
    header: Any
    non_empty_count: int
    empty_count: int
    sample_values: list[Any] = Field(default_factory=list)


class WorkbookProfile(BaseModel):
    source_path: Path
    sheet_name: Optional[str]
    row_count: int
    column_count: int
    columns: list[ColumnProfile] = Field(default_factory=list)


def excel_column_letter(index: int) -> str:
    """Convert a zero-based column index to an Excel column letter."""
    if index < 0:
        raise ValueError("Column index must be zero or greater")

    letters = ""
    number = index + 1

    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters

    return letters


def profile_workbook(
    source_path: Union[Path, str],
    sheet_name: Optional[str] = None,
) -> WorkbookProfile:
    path = Path(source_path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        rows = _read_csv(path)
        return _profile_rows(path, sheet_name=None, rows=rows)

    if suffix in {".xlsx", ".xlsm"}:
        resolved_sheet_name, rows = _read_xlsx(path, sheet_name)
        return _profile_rows(path, sheet_name=resolved_sheet_name, rows=rows)

    raise ValueError(f"Unsupported workbook type: {path.suffix}")


def profile_to_json(profile: WorkbookProfile) -> str:
    if hasattr(profile, "model_dump"):
        return json.dumps(profile.model_dump(mode="json"), indent=2)

    return profile.json(indent=2)


def _read_csv(path: Path) -> list[list[Any]]:
    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        return [row for row in csv.reader(csv_file)]


def _read_xlsx(path: Path, sheet_name: Optional[str]) -> tuple[str, list[list[Any]]]:
    with ZipFile(path) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = _read_workbook_relationships(archive)
        shared_strings = _read_shared_strings(archive)
        selected_sheet_name, sheet_path = _resolve_sheet_path(workbook, relationships, sheet_name)
        rows = _read_sheet_rows(archive, sheet_path, shared_strings)

    return selected_sheet_name, rows


def _read_workbook_relationships(archive: ZipFile) -> dict[str, str]:
    root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relationships: dict[str, str] = {}

    for relationship in root.findall(f"{{{PACKAGE_RELATIONSHIPS_NS}}}Relationship"):
        relationship_id = relationship.attrib["Id"]
        target = relationship.attrib["Target"]
        relationships[relationship_id] = _normalise_xlsx_part_path("xl", target)

    return relationships


def _resolve_sheet_path(
    workbook: ElementTree.Element,
    relationships: dict[str, str],
    sheet_name: Optional[str],
) -> tuple[str, str]:
    sheets = workbook.find(f"{{{SPREADSHEET_NS}}}sheets")
    if sheets is None:
        raise ValueError("Workbook does not contain any sheets")

    for sheet in sheets.findall(f"{{{SPREADSHEET_NS}}}sheet"):
        current_sheet_name = sheet.attrib["name"]
        if sheet_name is not None and current_sheet_name != sheet_name:
            continue

        relationship_id = sheet.attrib[f"{{{RELATIONSHIPS_NS}}}id"]
        return current_sheet_name, relationships[relationship_id]

    if sheet_name is None:
        raise ValueError("Workbook does not contain any sheets")

    raise KeyError(f"Sheet not found: {sheet_name}")


def _normalise_xlsx_part_path(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")

    return posixpath.normpath(posixpath.join(base_dir, target))


def _read_shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []

    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    shared_strings: list[str] = []

    for string_item in root.findall(f"{{{SPREADSHEET_NS}}}si"):
        text_parts = [
            text_node.text or ""
            for text_node in string_item.findall(f".//{{{SPREADSHEET_NS}}}t")
        ]
        shared_strings.append("".join(text_parts))

    return shared_strings


def _read_sheet_rows(
    archive: ZipFile,
    sheet_path: str,
    shared_strings: list[str],
) -> list[list[Any]]:
    root = ElementTree.fromstring(archive.read(sheet_path))
    sheet_data = root.find(f"{{{SPREADSHEET_NS}}}sheetData")
    if sheet_data is None:
        return []

    rows: list[list[Any]] = []
    for row in sheet_data.findall(f"{{{SPREADSHEET_NS}}}row"):
        row_values: list[Any] = []

        for cell in row.findall(f"{{{SPREADSHEET_NS}}}c"):
            cell_ref = cell.attrib.get("r")
            if cell_ref:
                column_index = _column_index_from_cell_ref(cell_ref)
                while len(row_values) <= column_index:
                    row_values.append(None)

                row_values[column_index] = _read_cell_value(cell, shared_strings)
            else:
                row_values.append(_read_cell_value(cell, shared_strings))

        rows.append(row_values)

    return rows


def _column_index_from_cell_ref(cell_ref: str) -> int:
    match = CELL_REF_PATTERN.match(cell_ref)
    if not match:
        raise ValueError(f"Invalid cell reference: {cell_ref}")

    index = 0
    for letter in match.group(1):
        index = index * 26 + (ord(letter) - 64)

    return index - 1


def _read_cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> Any:
    cell_type = cell.attrib.get("t")

    if cell_type == "inlineStr":
        return "".join(
            text_node.text or "" for text_node in cell.findall(f".//{{{SPREADSHEET_NS}}}t")
        )

    value_node = cell.find(f"{{{SPREADSHEET_NS}}}v")
    if value_node is None:
        return None

    raw_value = value_node.text or ""

    if cell_type == "s":
        return shared_strings[int(raw_value)]

    if cell_type == "b":
        return raw_value == "1"

    return raw_value


def _profile_rows(path: Path, sheet_name: Optional[str], rows: list[list[Any]]) -> WorkbookProfile:
    if not rows:
        return WorkbookProfile(
            source_path=path,
            sheet_name=sheet_name,
            row_count=0,
            column_count=0,
            columns=[],
        )

    header_row = rows[0]
    data_rows = rows[1:]
    column_count = max(len(row) for row in rows)

    columns: list[ColumnProfile] = []
    for index in range(column_count):
        header = header_row[index] if index < len(header_row) else None
        values = [row[index] if index < len(row) else None for row in data_rows]
        non_empty_values = [value for value in values if not _is_empty_like(value)]

        columns.append(
            ColumnProfile(
                index=index,
                excel_letter=excel_column_letter(index),
                header=header,
                non_empty_count=len(non_empty_values),
                empty_count=len(data_rows) - len(non_empty_values),
                sample_values=non_empty_values[:MAX_SAMPLE_VALUES],
            )
        )

    return WorkbookProfile(
        source_path=path,
        sheet_name=sheet_name,
        row_count=len(data_rows),
        column_count=column_count,
        columns=columns,
    )


def _is_empty_like(value: Any) -> bool:
    if value is None:
        return True

    if isinstance(value, float) and math.isnan(value):
        return True

    if isinstance(value, str):
        return value.strip().lower() in EMPTY_LIKE_STRINGS

    return False
