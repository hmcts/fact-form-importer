import json
import csv
from pathlib import Path
from types import SimpleNamespace

from fact_form_importer.ingest.column_mapping import excel_column_index, load_column_mapping
from fact_form_importer.cli import main
from fact_form_importer.llm.pipeline import LlmNormalisationResult, LlmUsageMetrics


def test_run_command_writes_processing_outputs(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FACT_DATA_API_BASE_URL", raising=False)
    monkeypatch.delenv("FACT_DATA_API_BEARER_TOKEN", raising=False)
    input_path = tmp_path / "sample.csv"
    output_path = tmp_path / "out"
    _write_minimal_forms_csv(input_path)

    exit_code = main(
        [
            "run",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--allow-local-vocabularies",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Run ID:" in captured.out
    assert "Validated submissions: 1" in captured.out
    assert "Duplicate court groups: 0" in captured.out
    assert "Duplicate affected records (included in needs human review): 0" in captured.out
    assert "Read-only approval users: 0" in captured.out
    assert "Excluded submitter users: 0" in captured.out
    assert "LLM enabled: False" in captured.out
    assert "Vocabulary source: local_json" in captured.out
    assert (output_path / "profile.json").exists()
    assert (output_path / "submissions_raw.json").exists()
    assert (output_path / "submissions_cleaned.json").exists()
    assert (output_path / "fact_payload.json").exists()
    assert (output_path / "import_summary.json").exists()
    assert (output_path / "nsu_cleaned_review.xlsx").exists()
    assert (output_path / "read_only_approval_users.json").exists()
    assert (output_path / "read_only_approval_users.xlsx").exists()

    summary = json.loads((output_path / "import_summary.json").read_text())
    assert summary["submission_count"] == 1
    assert summary["processed_count"] == 1
    assert summary["vocabulary_source"] == "local_json"
    assert summary["llm_enabled"] is False
    assert summary["llm_requested"] is False
    assert summary["llm_calls"] == 0


def test_run_command_rejects_llm_flag_when_env_circuit_breaker_is_disabled(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")

    exit_code = main(
        [
            "run",
            "--input",
            str(tmp_path / "does-not-need-to-exist.csv"),
            "--output",
            str(tmp_path / "out"),
            "--use-llm",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "--use-llm requires LLM_ENABLED=true" in captured.err


def test_run_command_requires_openai_configuration_when_llm_is_requested(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    exit_code = main(
        [
            "run",
            "--input",
            str(tmp_path / "does-not-need-to-exist.csv"),
            "--output",
            str(tmp_path / "out"),
            "--use-llm",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "OPENAI_BASE_URL is required for run --use-llm" in captured.err


def test_run_command_records_llm_metrics_when_explicitly_requested(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FACT_DATA_API_BASE_URL", raising=False)
    monkeypatch.delenv("FACT_DATA_API_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ai-foundry.example.test/openai/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "token")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.5")
    input_path = tmp_path / "sample.csv"
    output_path = tmp_path / "out"
    _write_minimal_forms_csv(input_path)
    calls = []

    def fake_normalise(submissions, field_rules, vocabularies, config):
        calls.append((submissions, field_rules, vocabularies, config))
        return LlmNormalisationResult(
            submissions=submissions,
            metrics=LlmUsageMetrics(calls=1, fields_selected=2, fields_processed=2),
        )

    monkeypatch.setattr("fact_form_importer.cli.normalise_submissions_with_llm", fake_normalise)

    exit_code = main(
        [
            "run",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--allow-local-vocabularies",
            "--use-llm",
        ]
    )

    captured = capsys.readouterr()
    summary = json.loads((output_path / "import_summary.json").read_text())

    assert exit_code == 0
    assert len(calls) == 1
    assert "LLM requested: True" in captured.out
    assert "LLM calls: 1" in captured.out
    assert "LLM fields processed: 2" in captured.out
    assert summary["llm_enabled"] is True
    assert summary["llm_requested"] is True
    assert summary["llm_calls"] == 1
    assert summary["llm_fields_selected"] == 2
    assert summary["llm_fields_processed"] == 2
    assert summary["llm_model"] == "gpt-5.5"


def test_run_command_requires_fact_api_base_url_by_default(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FACT_DATA_API_BASE_URL", raising=False)
    monkeypatch.delenv("FACT_DATA_API_BEARER_TOKEN", raising=False)
    input_path = tmp_path / "sample.csv"
    output_path = tmp_path / "out"
    _write_minimal_forms_csv(input_path)

    exit_code = main(["run", "--input", str(input_path), "--output", str(output_path)])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "FACT_DATA_API_BASE_URL is required for run" in captured.err


def test_llm_request_review_writes_selected_safe_requests_without_calling_model(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("FACT_DATA_API_BASE_URL", raising=False)
    monkeypatch.delenv("FACT_DATA_API_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("LLM_ENABLED", "false")
    input_path = tmp_path / "sample.csv"
    output_path = tmp_path / "out"
    _write_minimal_forms_csv(input_path, accessible_toilet_description="Available near reception.")

    exit_code = main(
        [
            "llm-request-review",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--allow-local-vocabularies",
        ]
    )

    captured = capsys.readouterr()
    review = json.loads((output_path / "llm_request_review.json").read_text())

    assert exit_code == 0
    assert "LLM request review records: 1" in captured.out
    assert "LLM request review fields: 1" in captured.out
    assert "LLM calls made: 0" in captured.out
    assert review["llm_enabled"] is False
    assert review["model_calls_made"] == 0
    assert review["request_count"] == 1
    assert review["field_count"] == 1
    assert "court_slug" not in review["requests"][0]
    assert review["requests"][0]["fields"] == [
        {
            "field": "facilities.accessible_toilet_description",
            "raw_value": "Available near reception.",
            "cleaned_value": "Available near reception.",
        }
    ]


def test_run_command_requires_fact_api_bearer_token_by_default(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact-data-api.example.test")
    monkeypatch.delenv("FACT_DATA_API_BEARER_TOKEN", raising=False)
    input_path = tmp_path / "sample.csv"
    output_path = tmp_path / "out"
    _write_minimal_forms_csv(input_path)

    exit_code = main(["run", "--input", str(input_path), "--output", str(output_path)])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "FACT_DATA_API_BEARER_TOKEN is required for run" in captured.err


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


def test_check_llm_command_reports_connection(capsys, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")

    def fake_check(config):
        return SimpleNamespace(
            base_url="https://ai-foundry.example.test/openai/v1",
            model="gpt-5.5",
            output_preview="OK",
        )

    monkeypatch.setattr("fact_form_importer.cli.check_llm_connection", fake_check)

    exit_code = main(["check-llm"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "LLM connection: OK" in captured.out
    assert "LLM enabled: False" in captured.out
    assert "OpenAI model: gpt-5.5" in captured.out


def test_check_llm_command_reports_errors(capsys, monkeypatch):
    def fake_check(config):
        raise ValueError("OPENAI_API_KEY is required for check-llm")

    monkeypatch.setattr("fact_form_importer.cli.check_llm_connection", fake_check)

    exit_code = main(["check-llm"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "OPENAI_API_KEY is required for check-llm" in captured.err


def test_llm_test_command_reports_structured_response(capsys, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.5")

    def fake_normalise(request, config):
        return SimpleNamespace(
            record_id=request.record_id,
            normalised_fields=[
                SimpleNamespace(
                    field="facilities.accessible_toilet_description",
                    value="Accessible toilet near reception.",
                    confidence="high",
                    needs_human_review=False,
                    reason="Preserved the stated location.",
                )
            ],
            confidence="high",
            needs_human_review=False,
            issues=[],
        )

    monkeypatch.setattr("fact_form_importer.cli.normalise_fields_with_llm", fake_normalise)

    exit_code = main(["llm-test"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "LLM normalisation test: OK" in captured.out
    assert "LLM called by this command: True" in captured.out
    assert "Pipeline LLM enabled for run: False" in captured.out
    assert "OpenAI model: gpt-5.5" in captured.out
    assert "Input fields:" in captured.out
    assert "raw: Ask security. Disabled toilet near reception." in captured.out
    assert "Output fields:" in captured.out
    assert "value: Accessible toilet near reception." in captured.out
    assert "Issues:" in captured.out
    assert "- None" in captured.out
    assert "Result:" in captured.out
    assert "confidence: high" in captured.out


def test_llm_test_command_reports_errors(capsys, monkeypatch):
    def fake_normalise(request, config):
        raise ValueError("OPENAI_API_KEY is required for normalise_fields_with_llm")

    monkeypatch.setattr("fact_form_importer.cli.normalise_fields_with_llm", fake_normalise)

    exit_code = main(["llm-test"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "OPENAI_API_KEY is required for normalise_fields_with_llm" in captured.err


def _write_minimal_forms_csv(path, accessible_toilet_description=None):
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
    if accessible_toilet_description is not None:
        _set(row, "J", accessible_toilet_description)

    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(header)
        writer.writerow(row)


def _set(row, column, value):
    row[excel_column_index(column)] = value
