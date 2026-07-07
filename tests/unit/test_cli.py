import json
import csv
from pathlib import Path

from fact_form_importer.ingest.column_mapping import excel_column_index, load_column_mapping
from fact_form_importer.cli import main


def test_run_command_writes_processing_outputs(tmp_path, capsys):
    input_path = tmp_path / "sample.csv"
    output_path = tmp_path / "out"
    _write_minimal_forms_csv(input_path)

    exit_code = main(["run", "--input", str(input_path), "--output", str(output_path)])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Run ID:" in captured.out
    assert "Validated submissions: 1" in captured.out
    assert (output_path / "profile.json").exists()
    assert (output_path / "submissions_raw.json").exists()
    assert (output_path / "submissions_cleaned.json").exists()
    assert (output_path / "fact_payload.json").exists()
    assert (output_path / "import_summary.json").exists()
    assert (output_path / "nsu_cleaned_review.xlsx").exists()

    summary = json.loads((output_path / "import_summary.json").read_text())
    assert summary["submission_count"] == 1
    assert summary["processed_count"] == 1


def test_run_command_returns_error_for_missing_file(tmp_path, capsys):
    exit_code = main(["run", "--input", str(tmp_path / "missing.csv"), "--output", str(tmp_path)])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error:" in captured.err


def test_profile_command_writes_profile_json(tmp_path, capsys):
    input_path = tmp_path / "sample.csv"
    output_path = tmp_path / "out"
    input_path.write_text("ID,Name\n1,Example\n", encoding="utf-8")

    exit_code = main(["profile", "--input", str(input_path), "--output", str(output_path)])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Workbook profile" in captured.out
    assert (output_path / "profile.json").exists()


def test_profile_command_returns_error_for_missing_file(tmp_path, capsys):
    exit_code = main(["profile", "--input", str(tmp_path / "missing.csv")])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error:" in captured.err


def test_profile_command_returns_error_for_unsupported_file(tmp_path, capsys):
    input_path = tmp_path / "sample.txt"
    input_path.write_text("not a spreadsheet", encoding="utf-8")

    exit_code = main(["profile", "--input", str(input_path)])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Unsupported workbook type" in captured.err


def test_ingest_command_writes_outputs(tmp_path, capsys):
    input_path = tmp_path / "sample.csv"
    output_path = tmp_path / "out"
    input_path.write_text("ID,Start time\n1,2026-01-01\n", encoding="utf-8")

    exit_code = main(["ingest", "--input", str(input_path), "--output", str(output_path)])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Ingested submissions" in captured.out
    assert (output_path / "ingest_summary.json").exists()


def test_ingest_command_returns_error_for_missing_file(tmp_path, capsys):
    exit_code = main(["ingest", "--input", str(tmp_path / "missing.csv"), "--output", str(tmp_path)])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error:" in captured.err


def _write_minimal_forms_csv(path):
    mapping = load_column_mapping(Path("config/column_mapping.json"))
    columns = mapping.expected_columns()
    max_index = max(excel_column_index(column.column) for column in columns)
    header = [""] * (max_index + 1)
    row = [""] * (max_index + 1)

    for column in columns:
        header[excel_column_index(column.column)] = column.expected_header or column.field

    _set(row, "A", "1")
    _set(row, "G", "example-court")
    _set(row, "H", "Yes")

    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(header)
        writer.writerow(row)


def _set(row, column, value):
    row[excel_column_index(column)] = value
