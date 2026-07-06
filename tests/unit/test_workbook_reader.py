import csv
import json
from pathlib import Path

from fact_form_importer.ingest.column_mapping import excel_column_index, load_column_mapping
from fact_form_importer.ingest.workbook_reader import ingest_workbook


def test_ingest_workbook_creates_submissions_and_outputs(tmp_path):
    mapping = load_column_mapping(Path("config/column_mapping.json"))
    csv_path = tmp_path / "submissions.csv"
    output_path = tmp_path / "out"
    rows = _build_ingest_rows(mapping)

    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerows(rows)

    result = ingest_workbook(csv_path, output_path)

    assert len(result.submissions) == 2
    assert result.skipped_empty_rows == 1
    assert result.mapping_warnings == []

    valid_submission = result.submissions[0]
    assert valid_submission.source.source_row_number == 2
    assert valid_submission.court_slug == "fleetwood-court"
    assert valid_submission.facilities["accessible_parking"] is True
    assert valid_submission.facilities["food_and_drink"] == [
        "Free water dispensers",
        "Drink vending machines",
    ]
    assert valid_submission.translation_email == "info@example.test"
    assert valid_submission.addresses[0].postcode == "SW1A 1AA"
    assert valid_submission.contacts[0].email == "contact@example.test"
    assert valid_submission.opening_hours[0].monday_to_friday.open == "09:30"
    assert valid_submission.status == "processed"

    failed_submission = result.submissions[1]
    assert failed_submission.status == "failed"
    assert failed_submission.issues[0].code == "MISSING_COURT_IDENTIFIER"

    assert (output_path / "submissions_raw.json").exists()
    assert (output_path / "submissions_cleaned.json").exists()
    assert (output_path / "ingest_summary.json").exists()

    summary = json.loads((output_path / "ingest_summary.json").read_text())
    assert summary["submissions_total"] == 2
    assert summary["skipped_empty_rows"] == 1
    assert summary["failed"] == 1


def _build_ingest_rows(mapping):
    columns = mapping.expected_columns()
    max_index = max(excel_column_index(column.column) for column in columns)
    header = [""] * (max_index + 1)
    valid = [""] * (max_index + 1)
    missing_slug = [""] * (max_index + 1)
    metadata_only = [""] * (max_index + 1)

    for column in columns:
        header[excel_column_index(column.column)] = column.expected_header or column.field

    _set(valid, "A", "1")
    _set(valid, "B", "2026-01-01 09:00")
    _set(valid, "C", "2026-01-01 09:30")
    _set(valid, "D", "submitter@example.test")
    _set(valid, "E", "Submitter")
    _set(valid, "G", "https://www.find-court-tribunal.service.gov.uk/courts/Fleetwood Court")
    _set(valid, "H", "Yes")
    _set(valid, "I", "02079460000")
    _set(valid, "J", " Ground floor ")
    _set(valid, "K", "No")
    _set(valid, "L", "02079460001")
    _set(valid, "M", "Hearing loop systems are available at this court.")
    _set(valid, "N", "Yes")
    _set(valid, "O", "90")
    _set(valid, "P", "1000")
    _set(valid, "Q", "No")
    _set(valid, "R", "Yes")
    _set(valid, "S", "Free water dispensers; Drink vending machines")
    _set(valid, "T", "Yes")
    _set(valid, "U", "No")
    _set(valid, "V", "Yes")
    _set(valid, "W", "No")
    _set(valid, "X", "Yes")
    _set(valid, "Y", "02079460002")
    _set(valid, "Z", "INFO@EXAMPLE.TEST")
    _set(valid, "AA", "Visit")
    _set(valid, "AB", "1 Example Street")
    _set(valid, "AD", "London")
    _set(valid, "AF", "sw1a1aa")
    _set(valid, "AG", "Civil; Crime")
    _set(valid, "AH", "County Court")
    _set(valid, "BS", "County Court")
    _set(valid, "BT", "Forms; Documents")
    _set(valid, "BV", "Yes")
    _set(valid, "BW", "09")
    _set(valid, "BX", "00")
    _set(valid, "BY", "17")
    _set(valid, "BZ", "00")
    _set(valid, "CU", "Yes")
    _set(valid, "CV", "2")
    _set(valid, "CW", "02079460003")
    _set(valid, "CX", "Enquiries")
    _set(valid, "CZ", "02079460004")
    _set(valid, "DA", "contact@example.test")
    _set(valid, "EU", "Court open")
    _set(valid, "EV", "Yes")
    _set(valid, "EW", "9")
    _set(valid, "EX", "30")
    _set(valid, "EY", "17")
    _set(valid, "EZ", "00")

    _set(missing_slug, "A", "2")
    _set(missing_slug, "H", "Yes")

    _set(metadata_only, "A", "3")
    _set(metadata_only, "D", "metadata-only@example.test")

    return [header, valid, missing_slug, metadata_only]


def _set(row, column, value):
    row[excel_column_index(column)] = value
