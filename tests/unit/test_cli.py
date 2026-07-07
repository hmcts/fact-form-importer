from fact_form_importer.cli import main


def test_run_command_prints_placeholder(capsys):
    exit_code = main(["run", "--input", "./spreadsheet.xlsx", "--output", "./out"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "fact-form-importer placeholder" in captured.out
    assert "spreadsheet.xlsx" in captured.out
    assert "out" in captured.out


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
