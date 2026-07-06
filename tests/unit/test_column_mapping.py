from pathlib import Path

from fact_form_importer.ingest.column_mapping import (
    build_raw_row,
    get_cell,
    headers_broadly_match,
    load_column_mapping,
)


def test_load_column_mapping_has_known_layout_sections():
    mapping = load_column_mapping(Path("config/column_mapping.json"))

    assert mapping.version == 1
    assert mapping.metadata[0].column == "A"
    assert mapping.scalars[0].column == "G"
    assert len(mapping.address_groups) == 5
    assert mapping.address_groups[0].columns[0].column == "AA"
    assert mapping.contact_detail_groups[-1].columns[-1].column == "ET"
    assert mapping.opening_hours_groups[-1].columns[-1].column == "GV"
    assert mapping.warnings == []


def test_headers_broadly_match_allows_forms_suffixes_and_long_prompt_text():
    assert headers_broadly_match("Address line 12", "Address line 1")
    assert headers_broadly_match("Choose a description for the contact details10", "Choose a description for the contact details")
    assert headers_broadly_match(
        "Enter the court slug (the last part of the court's web address).",
        "Enter the court slug",
    )


def test_validate_headers_warns_for_missing_unexpected_and_mismatched_columns():
    mapping = load_column_mapping(Path("config/column_mapping.json"))
    headers = {
        column_ref.column: column_ref.expected_header for column_ref in mapping.expected_columns()
    }
    headers.pop("G")
    headers["H"] = "Unexpected accessible parking heading"
    headers["GW"] = "Unexpected extra field"

    warnings = mapping.validate_headers(headers)
    warning_codes = {warning.code for warning in warnings}

    assert "missing_column" in warning_codes
    assert "header_mismatch" in warning_codes
    assert "unexpected_column" in warning_codes
    assert any(warning.column == "G" for warning in warnings)
    assert any(warning.column == "H" for warning in warnings)
    assert any(warning.column == "GW" for warning in warnings)


def test_get_cell_supports_lists_and_letter_keyed_dicts():
    row = ["id", "start", "completion"]

    assert get_cell(row, "A") == "id"
    assert get_cell(row, "C") == "completion"
    assert get_cell(row, "D") is None
    assert get_cell({"AA": "address type"}, "AA") == "address type"


def test_build_raw_row_returns_excel_letter_keys():
    assert build_raw_row(["id", "start", "completion"]) == {
        "A": "id",
        "B": "start",
        "C": "completion",
    }
    assert build_raw_row({"a": "id"}) == {"A": "id"}
