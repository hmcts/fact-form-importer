from fact_form_importer.cli import main


def test_run_command_prints_placeholder(capsys):
    exit_code = main(["run", "--input", "./spreadsheet.xlsx", "--output", "./out"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "fact-form-importer placeholder" in captured.out
    assert "spreadsheet.xlsx" in captured.out
    assert "out" in captured.out
