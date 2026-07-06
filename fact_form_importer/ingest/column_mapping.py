"""Column mapping configuration and validation helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

LETTER_PATTERN = re.compile(r"^[A-Z]+$")


class MappingWarning(BaseModel):
    code: str
    message: str
    column: Optional[str] = None
    expected_header: Optional[str] = None
    actual_header: Optional[str] = None


class ColumnRef(BaseModel):
    field: str
    column: str
    expected_header: Optional[str] = None


class RepeatedGroup(BaseModel):
    index: int
    columns: list[ColumnRef] = Field(default_factory=list)


class ColumnMapping(BaseModel):
    version: int
    description: Optional[str] = None
    metadata: list[ColumnRef] = Field(default_factory=list)
    scalars: list[ColumnRef] = Field(default_factory=list)
    address_groups: list[RepeatedGroup] = Field(default_factory=list)
    counter_service: list[ColumnRef] = Field(default_factory=list)
    interview_rooms: list[ColumnRef] = Field(default_factory=list)
    contact_detail_groups: list[RepeatedGroup] = Field(default_factory=list)
    opening_hours_groups: list[RepeatedGroup] = Field(default_factory=list)
    warnings: list[MappingWarning] = Field(default_factory=list)

    def expected_columns(self) -> list[ColumnRef]:
        columns: list[ColumnRef] = []
        columns.extend(self.metadata)
        columns.extend(self.scalars)
        for group in self.address_groups:
            columns.extend(group.columns)
        columns.extend(self.counter_service)
        columns.extend(self.interview_rooms)
        for group in self.contact_detail_groups:
            columns.extend(group.columns)
        for group in self.opening_hours_groups:
            columns.extend(group.columns)
        return columns

    def validate_headers(self, headers_by_column: dict[str, Any]) -> list[MappingWarning]:
        warnings: list[MappingWarning] = []
        normalised_headers = {column.upper(): value for column, value in headers_by_column.items()}

        for column_ref in self.expected_columns():
            actual_header = normalised_headers.get(column_ref.column)
            if actual_header is None:
                warnings.append(
                    MappingWarning(
                        code="missing_column",
                        column=column_ref.column,
                        expected_header=column_ref.expected_header,
                        message=f"Expected column {column_ref.column} is missing from workbook headers",
                    )
                )
                continue

            if column_ref.expected_header and not headers_broadly_match(
                str(actual_header),
                column_ref.expected_header,
            ):
                warnings.append(
                    MappingWarning(
                        code="header_mismatch",
                        column=column_ref.column,
                        expected_header=column_ref.expected_header,
                        actual_header=str(actual_header),
                        message=(
                            f"Column {column_ref.column} header differs from expected mapping"
                        ),
                    )
                )

        expected_letters = {column_ref.column for column_ref in self.expected_columns()}
        for column, actual_header in normalised_headers.items():
            if column not in expected_letters:
                warnings.append(
                    MappingWarning(
                        code="unexpected_column",
                        column=column,
                        actual_header=None if actual_header is None else str(actual_header),
                        message=f"Workbook contains unmapped column {column}",
                    )
                )

        self.warnings = warnings
        return warnings


def load_column_mapping(path: Path) -> ColumnMapping:
    mapping = ColumnMapping(**json.loads(path.read_text(encoding="utf-8")))
    mapping.warnings = _validate_mapping_config(mapping)
    return mapping


def get_cell(row: Any, column_letter: str) -> Any:
    column = column_letter.upper()
    if isinstance(row, dict):
        return row.get(column)

    index = excel_column_index(column)
    if index >= len(row):
        return None

    return row[index]


def build_raw_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return {str(column).upper(): value for column, value in row.items()}

    return {excel_column_letter(index): value for index, value in enumerate(row)}


def headers_broadly_match(actual_header: str, expected_header: str) -> bool:
    actual = _normalise_header(actual_header)
    expected = _normalise_header(expected_header)

    if actual == expected:
        return True

    # Microsoft Forms commonly appends numeric suffixes to duplicate/repeated headers.
    if actual.startswith(expected):
        remainder = actual[len(expected) :].strip()
        return not remainder or remainder.isdigit() or remainder.startswith(
            ("(", ".", "for example", "if so")
        )

    return actual == _strip_forms_suffix(expected) or _strip_forms_suffix(actual) == expected


def excel_column_index(column_letter: str) -> int:
    column = column_letter.upper()
    if not LETTER_PATTERN.match(column):
        raise ValueError(f"Invalid Excel column letter: {column_letter}")

    index = 0
    for letter in column:
        index = index * 26 + (ord(letter) - 64)

    return index - 1


def excel_column_letter(index: int) -> str:
    if index < 0:
        raise ValueError("Column index must be zero or greater")

    letters = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters

    return letters


def _validate_mapping_config(mapping: ColumnMapping) -> list[MappingWarning]:
    warnings: list[MappingWarning] = []
    seen: dict[str, str] = {}

    for column_ref in mapping.expected_columns():
        if column_ref.column in seen:
            warnings.append(
                MappingWarning(
                    code="duplicate_mapping_column",
                    column=column_ref.column,
                    message=(
                        f"Column {column_ref.column} is mapped to both "
                        f"{seen[column_ref.column]} and {column_ref.field}"
                    ),
                )
            )
        seen[column_ref.column] = column_ref.field

    return warnings


def _normalise_header(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").strip().lower().split())


def _strip_forms_suffix(value: str) -> str:
    return re.sub(r"(?<!\d)\d+$", "", value).strip()
