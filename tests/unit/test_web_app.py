import io
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_STORED, ZipFile

from fact_form_importer.config import AppConfig
from fact_form_importer.execution.fact_api import ApiResponse
from fact_form_importer.execution.models import ExecutionLedger
from fact_form_importer.execution.service import ApiExecutionService
from fact_form_importer.output.archive import publish_run_archive, stage_path
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.web.app import (
    LocalJobRunner,
    _action_evidence,
    _action_execution_status,
    _load_readiness_report,
    _raw_evidence_for_fields,
    _safe_job_error,
    _value_at_path,
    create_app,
    run_server,
)


def test_review_ui_lists_archives_and_displays_record_raw_data(tmp_path):
    output_root, run_id = _archive(tmp_path)
    app = create_app(output_root, config=AppConfig())
    client = app.test_client()

    assert client.get("/").status_code == 200
    assert run_id.encode() in client.get("/").data
    run_page = client.get(f"/runs/{run_id}")
    assert run_page.status_code == 200
    assert b"Download this run (.zip)" in run_page.data
    assert client.get(f"/runs/{run_id}/records?status=processed").status_code == 200
    review = client.get(f"/runs/{run_id}/records?status=needs_human_review")
    assert b"review-court" in review.data
    detail = client.get(f"/runs/{run_id}/records/2")
    assert detail.status_code == 200
    assert b"Raw submitted values" in detail.data
    assert client.get(f"/runs/{run_id}/issues").status_code == 200
    assert b"LLM Review Factors" in client.get(f"/runs/{run_id}/llm-review-factors").data
    assert b"OS-Held Address Actions" in client.get(f"/runs/{run_id}/os-address-factors").data
    assert client.get(f"/runs/{run_id}/api-actions?readiness=ready").status_code == 200
    execution_page = client.get(f"/runs/{run_id}/execution-summary")
    assert execution_page.status_code == 200
    assert b"Attention by API request type" in execution_page.data
    execution_json = client.get(f"/runs/{run_id}/execution-summary.json")
    assert execution_json.status_code == 200
    assert execution_json.headers["Content-Disposition"].startswith("attachment")
    assert b"Duplicate form decision workbook" in client.get(f"/runs/{run_id}").data
    assert b"LLM review rows" in client.get("/").data
    assert b"OS-held address rows" in client.get("/").data


def test_llm_actions_page_displays_evidence_and_approves_without_executing(tmp_path):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    review_id = "llm-field-2-test"
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps(
            {
                "review_version": "1.0",
                "item_count": 1,
                "field_item_count": 1,
                "address_item_count": 0,
                "actionable_item_count": 1,
                "items": [
                    {
                        "review_id": review_id,
                        "kind": "field",
                        "source_row_number": 2,
                        "court_slug": "example-court",
                        "field": "facilities.food_and_drink",
                        "source_raw_values": {"S": "water"},
                        "llm_input": {"raw_value": "water", "cleaned_value": "water"},
                        "model_result": {
                            "value": ["Free water dispensers"],
                            "confidence": "high",
                            "needs_human_review": False,
                            "reason": "Exact vocabulary mapping",
                        },
                        "outcome": "accepted",
                        "dependent_action_ids": ["example-court-1"],
                        "actionable": True,
                    }
                ],
            }
        )
    )
    execution_client = _FakeExecutionClient()
    service = ApiExecutionService(output_root, AppConfig(), execution_client)
    client = create_app(output_root, execution_service=service).test_client()

    page = client.get(f"/runs/{run_id}/llm-actions")

    assert page.status_code == 200
    assert b"LLM Actions Review" in page.data
    assert b"Free water dispensers" in page.data
    assert b"Approve" in page.data

    approved = client.post(f"/runs/{run_id}/llm-actions/{review_id}/approve")

    assert approved.status_code == 302
    assert execution_client.writes == []
    refreshed = client.get(f"/runs/{run_id}/llm-actions")
    assert b"Approved" in refreshed.data
    assert service.get_execution_summary(run_id)["llm_approval_counts"]["approved"] == 1


def test_llm_actions_page_labels_strict_address_policy_approval(tmp_path):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    readiness_path = archive_path / "api_readiness_report.json"
    readiness = json.loads(readiness_path.read_text())
    readiness["records"][0]["actions"][0]["resource"] = "address"
    readiness_path.write_text(json.dumps(readiness))
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps(
            {
                "review_version": "1.0",
                "item_count": 1,
                "field_item_count": 0,
                "address_item_count": 1,
                "actionable_item_count": 1,
                "items": [
                    {
                        "review_id": "address-review",
                        "kind": "address",
                        "source_row_number": 2,
                        "court_slug": "example-court",
                        "field": "addresses[1]",
                        "address_index": 1,
                        "submitted_address": {"line_1": "Submitted Court"},
                        "source_raw_values": {"Address": "Submitted Court"},
                        "llm_input": {"candidates": [{"uprn": "uprn-1"}]},
                        "os_candidates": [{"uprn": "uprn-1"}],
                        "model_result": {
                            "uprn": "uprn-1",
                            "confidence": "high",
                            "needs_human_review": False,
                            "reason": "The sole candidate consistently matches.",
                        },
                        "outcome": "accepted",
                        "dependent_action_ids": ["example-court-1"],
                        "actionable": True,
                    }
                ],
            }
        )
    )
    execution_client = _FakeExecutionClient()
    service = ApiExecutionService(output_root, AppConfig(), execution_client)
    client = create_app(output_root, execution_service=service).test_client()

    page = client.get(f"/runs/{run_id}/llm-actions")
    summary = client.get(f"/runs/{run_id}/execution-summary")

    assert page.status_code == 200
    assert b"Automatically approved" in page.data
    assert b"high-single-os-candidate-v1" in page.data
    assert b"Addresses auto-approved" in summary.data
    assert service.get_execution_summary(run_id)["llm_approval_counts"]["auto_approved"] == 1
    assert execution_client.writes == []


def test_review_ui_allows_only_manifested_artifact_downloads(tmp_path):
    output_root, run_id = _archive(tmp_path)
    client = create_app(output_root).test_client()

    response = client.get(f"/runs/{run_id}/download/import_summary.json")
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith("attachment")
    assert client.get(f"/runs/{run_id}/download/../.env").status_code == 404


def test_review_ui_downloads_a_complete_run_zip(tmp_path):
    output_root, run_id = _archive(tmp_path)
    client = create_app(output_root).test_client()

    response = client.get(f"/runs/{run_id}/download/archive.zip")

    assert response.status_code == 200
    assert response.headers["Content-Disposition"].endswith(f"{run_id}.zip")
    with ZipFile(io.BytesIO(response.data)) as archive:
        assert "run_manifest.json" in archive.namelist()
        assert "import_summary.json" in archive.namelist()
        assert "nsu_cleaned_review.xlsx" in archive.namelist()
        assert archive.getinfo("import_summary.json").compress_type == ZIP_STORED


def test_review_ui_downloads_reports_from_a_relative_output_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    output_root, run_id = _archive_at_path(Path("relative-out"))
    client = create_app(output_root).test_client()

    assert client.get(f"/runs/{run_id}/download/import_summary.json").status_code == 200
    assert client.get(f"/runs/{run_id}/download/nsu_cleaned_review.xlsx").status_code == 200


def test_review_ui_rejects_invalid_uploads_and_runs_one_background_job(tmp_path):
    calls = []

    def fake_processor(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(run_id="completed-run")

    app = create_app(tmp_path / "out", processor=fake_processor)
    client = app.test_client()

    invalid = client.post(
        "/runs",
        data={"source_file": (io.BytesIO(b"bad"), "forms.txt")},
        content_type="multipart/form-data",
    )
    assert invalid.status_code == 400

    started = client.post(
        "/runs",
        data={"source_file": (io.BytesIO(b"a,b\n"), "forms.csv")},
        content_type="multipart/form-data",
    )
    assert started.status_code == 302
    job_id = started.headers["Location"].rsplit("/", 1)[-1]
    assert client.get(f"/jobs/{job_id}").status_code == 200
    app.config["JOB_RUNNER"].executor.shutdown(wait=True)
    status = client.get(f"/jobs/{job_id}/status")
    assert status.status_code == 200
    assert status.get_json()["state"] == "completed"
    assert calls[0]["source_name"] == "forms.csv"
    assert calls[0]["verify_addresses"] is False
    assert not list((tmp_path / "out" / ".uploads").rglob("forms.csv"))


def test_review_ui_rejects_llm_upload_when_circuit_breaker_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")
    client = create_app(tmp_path / "out").test_client()

    response = client.post(
        "/runs",
        data={"source_file": (io.BytesIO(b"a,b\n"), "forms.csv"), "use_llm": "on"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"LLM processing is disabled" in response.data


def test_review_ui_rejects_address_verification_without_fact_api_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("FACT_DATA_API_BASE_URL", raising=False)
    monkeypatch.delenv("FACT_DATA_API_BEARER_TOKEN", raising=False)
    client = create_app(tmp_path / "out", config=AppConfig()).test_client()

    response = client.post(
        "/runs",
        data={
            "source_file": (io.BytesIO(b"a,b\n"), "forms.csv"),
            "verify_addresses": "on",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"Address verification requires" in response.data


def test_review_ui_handles_missing_resources_filters_and_pagination(tmp_path):
    output_root, run_id = _archive(tmp_path)
    client = create_app(output_root).test_client()

    assert client.get("/runs/missing").status_code == 404
    assert client.get("/jobs/missing").status_code == 404
    assert client.get("/runs/missing-records/records").status_code == 404
    assert client.get(f"/runs/{run_id}/records/999").status_code == 404
    assert client.get(f"/runs/{run_id}/issues?code=UNKNOWN").status_code == 200
    assert client.get(f"/runs/{run_id}/records?q=2&page=not-a-page").status_code == 200
    assert client.get(f"/runs/{run_id}/download/not-in-manifest.json").status_code == 404


def test_local_job_runner_restores_interrupted_job_and_server_refuses_network_bind(tmp_path):
    output_root = tmp_path / "out"
    jobs_path = output_root / ".jobs"
    jobs_path.mkdir(parents=True)
    (jobs_path / "interrupted.json").write_text(
        json.dumps(
            {
                "job_id": "interrupted",
                "state": "running",
                "source_name": "forms.csv",
                "use_llm": False,
            }
        )
    )

    runner = LocalJobRunner(output_root, lambda **kwargs: SimpleNamespace(run_id="unused"))
    assert runner.get("interrupted").state == "failed"

    try:
        run_server(output_root, host="0.0.0.0")
    except ValueError as exc:
        assert "localhost" in str(exc)
    else:
        raise AssertionError("Expected localhost bind validation")


def test_local_job_runner_rejects_concurrent_jobs_and_records_processing_failure(tmp_path):
    def fails(**kwargs):
        raise RuntimeError("no details should be stored")

    output_root = tmp_path / "out"
    app = create_app(output_root, processor=fails)
    runner = app.config["JOB_RUNNER"]
    runner.active_job_id = "already-running"
    client = app.test_client()
    response = client.post(
        "/runs",
        data={"source_file": (io.BytesIO(b"a"), "forms.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert b"already running" in response.data
    runner.active_job_id = None

    response = client.post(
        "/runs",
        data={"source_file": (io.BytesIO(b"a"), "forms.csv")},
        content_type="multipart/form-data",
    )
    job_id = response.headers["Location"].rsplit("/", 1)[-1]
    runner.executor.shutdown(wait=True)
    assert (
        client.get(f"/jobs/{job_id}/status").get_json()["error"]
        == "Processing failed (RuntimeError)"
    )


def test_review_ui_explains_failing_fact_api_authentication_without_exposing_response_details():
    error = ValueError("Unable to load FaCT API vocabularies: Client error '401 Unauthorized'")

    assert _safe_job_error(error) == (
        "FaCT API authentication failed. Refresh FACT_DATA_API_BEARER_TOKEN and restart the review UI."
    )


def test_review_ui_handles_missing_upload_and_json_fallback(tmp_path):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    (archive_path / "issue_report.json").unlink()
    client = create_app(output_root).test_client()

    assert client.post("/runs", data={}, content_type="multipart/form-data").status_code == 400
    assert client.get(f"/runs/{run_id}/issues").status_code == 200


def test_review_ui_checks_actions_but_refuses_writes_when_circuit_breaker_is_off(
    tmp_path, monkeypatch
):
    output_root, run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "false")
    execution = ApiExecutionService(output_root, AppConfig(), _FakeExecutionClient())
    client = create_app(output_root, config=AppConfig(), execution_service=execution).test_client()

    detail = client.get(f"/runs/{run_id}/records/2")
    assert b"Check target sections" in detail.data
    assert b"Writes are disabled locally" in detail.data

    checked = client.post(
        f"/runs/{run_id}/courts/example-court/api-check", data={"source_row_number": "2"}
    )
    assert checked.status_code == 302
    assert client.get(f"/runs/{run_id}/records/2").data.count(b"ready") >= 1

    execute = client.post(
        f"/runs/{run_id}/courts/example-court/actions/example-court-1/execute",
        data={"source_row_number": "2"},
    )
    assert execute.status_code == 403


def test_review_ui_executes_a_preflight_safe_action_when_explicitly_enabled(tmp_path, monkeypatch):
    output_root, run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    execution_client = _FakeExecutionClient()
    execution = ApiExecutionService(output_root, AppConfig(), execution_client)
    client = create_app(output_root, config=AppConfig(), execution_service=execution).test_client()

    response = client.post(
        f"/runs/{run_id}/courts/example-court/actions/example-court-1/execute",
        data={"source_row_number": "2"},
    )

    assert response.status_code == 302
    assert execution_client.writes == [
        (
            "POST",
            "/courts/id/v1/building-facilities",
            {
                "parking": True,
                "freeWaterDispensers": False,
                "snackVendingMachines": False,
                "drinkVendingMachines": False,
                "cafeteria": False,
                "waitingArea": False,
                "quietRoom": False,
                "babyChanging": False,
                "wifi": False,
                "courtId": "id",
            },
        )
    ]
    assert b"succeeded" in client.get(f"/runs/{run_id}/records/2").data


def test_review_ui_executes_all_safe_actions_and_handles_execution_errors(tmp_path, monkeypatch):
    output_root, run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    execution_client = _FakeExecutionClient()
    execution = ApiExecutionService(output_root, AppConfig(), execution_client)
    client = create_app(output_root, config=AppConfig(), execution_service=execution).test_client()

    success = client.post(
        f"/runs/{run_id}/courts/example-court/execute-safe", data={"source_row_number": "2"}
    )
    assert success.status_code == 302
    assert execution_client.writes

    failing_client = create_app(
        output_root, config=AppConfig(), execution_service=_FailingExecutionService()
    ).test_client()
    assert (
        failing_client.post(
            f"/runs/{run_id}/courts/example-court/api-check", data={"source_row_number": "2"}
        ).status_code
        == 400
    )
    assert (
        failing_client.post(
            f"/runs/{run_id}/courts/example-court/actions/example-court-1/execute",
            data={"source_row_number": "2"},
        ).status_code
        == 400
    )
    assert (
        failing_client.post(
            f"/runs/{run_id}/courts/example-court/execute-safe", data={"source_row_number": "2"}
        ).status_code
        == 400
    )
    assert failing_client.post(f"/runs/{run_id}/execute-safe").status_code == 400


def test_review_ui_executes_all_safe_actions_for_a_run_and_shows_summary(tmp_path, monkeypatch):
    output_root, run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    execution_client = _FakeExecutionClient()
    execution = ApiExecutionService(output_root, AppConfig(), execution_client)
    client = create_app(output_root, config=AppConfig(), execution_service=execution).test_client()

    assert b"Run all safe actions" in client.get(f"/runs/{run_id}").data
    response = client.post(f"/runs/{run_id}/execute-safe")

    assert response.status_code == 302
    assert execution_client.writes
    summary = client.get(f"/runs/{run_id}/execution-summary")
    assert b"Succeeded actions" in summary.data
    assert b"completed" in summary.data


def test_action_evidence_projects_cleaned_and_raw_fields(monkeypatch, tmp_path):
    submission = {
        "facilities": {"parking_available": True},
        "translation_phone": "020 7946 0000",
        "counter_service": {"assists_with": ["Forms"]},
        "interview_rooms": {"has_interview_rooms": True},
        "addresses": [{"index": 1, "line_1": "1 Main Street"}],
        "contacts": [{"index": 1, "email": "contact@example.test"}],
        "opening_hours": [{"index": 1, "type": "Court open"}],
        "raw": {
            "R": "Yes",
            "Y": "02079460000",
            "BS": "Forms",
            "CU": "Yes",
            "AA": "Visit",
            "AB": "1 Main Street",
            "CX": "Enquiries",
            "EA": "contact@example.test",
            "EU": "Court open",
        },
    }
    fields = [
        "facilities.parking_available",
        "translation_phone",
        "counter_service",
        "interview_rooms",
        "addresses[1]",
        "contacts[1]",
        "opening_hours[1]",
    ]

    evidence = _action_evidence(
        submission,
        {
            "source_fields": fields,
            "migration_assumptions": ["Approved migration default"],
        },
    )

    assert evidence["cleaned"]["addresses[1]"]["line_1"] == "1 Main Street"
    assert evidence["cleaned"]["contacts[1]"]["email"] == "contact@example.test"
    assert evidence["raw"]["R"] == "Yes"
    assert evidence["raw"]["AB"] == "1 Main Street"
    assert evidence["address_verification"] is None
    assert evidence["request_body_normalisations"] == {}
    assert evidence["migration_assumptions"] == ["Approved migration default"]
    assert _value_at_path({}, "missing.value") is None
    assert _raw_evidence_for_fields({"A": "value"}, ["addresses[bad]"]) == {}

    assert _raw_evidence_for_fields(["not", "a", "mapping"], ["facilities.parking_available"]) == [
        "not",
        "a",
        "mapping",
    ]


def test_action_execution_status_handles_missing_ledger_values(tmp_path):
    ledger = ApiExecutionService(tmp_path).get_ledger("unknown")
    assert _action_execution_status(ledger, None, None) == "planned"


def test_legacy_readiness_download_missing_file_and_server_runner(tmp_path, monkeypatch):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    (archive_path / "api_readiness_report.json").unlink()
    (archive_path / "fact_api_import_manifest.json").write_text(json.dumps({"records": []}))
    assert _load_readiness_report(archive_path) == {"records": []}
    client = create_app(output_root).test_client()
    (archive_path / "import_summary.json").unlink()
    assert client.get(f"/runs/{run_id}/download/import_summary.json").status_code == 404

    calls = []

    class FakeApp:
        def run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("fact_form_importer.web.app.create_app", lambda output: FakeApp())
    run_server(output_root, host="localhost", port=5050)
    assert calls == [{"host": "localhost", "port": 5050, "debug": False}]


def _archive(tmp_path):
    output_root = tmp_path / "out"
    return _archive_at_path(output_root)


def _archive_at_path(output_root):
    run_id = "20260710T120000Z-test"
    staging = stage_path(output_root, run_id)
    staging.mkdir(parents=True)
    submission = {
        "source": {"source_row_number": 2},
        "court_slug": "example-court",
        "court_slug_raw": "Example Court",
        "status": "processed",
        "raw": {"court_slug": "Example Court"},
        "issues": [],
    }
    review_submission = {
        "source": {"source_row_number": 3},
        "court_slug": "review-court",
        "court_slug_raw": "Review Court",
        "status": "needs_human_review",
        "raw": {"court_slug": "Review Court"},
        "issues": [
            {"code": "DUPLICATE_COURT_SLUG"},
            {
                "field": "facilities.accessible_toilet_description",
                "code": "LLM_LOW_CONFIDENCE",
                "message": "LLM normalisation confidence was not high",
            },
        ],
    }
    summary = {
        "submission_count": 2,
        "unique_court_slug_count": 2,
        "processed_count": 1,
        "processed_with_warnings_count": 0,
        "needs_human_review_count": 1,
        "failed_count": 0,
        "duplicate_slug_affected_record_count": 0,
        "api_manifest_ready_action_count": 1,
        "api_manifest_pending_action_count": 0,
        "llm_calls": 0,
        "llm_requested": False,
    }
    (staging / "submissions_cleaned.json").write_text(json.dumps([submission, review_submission]))
    (staging / "issue_report.json").write_text("[]")
    (staging / "address_verification_report.json").write_text(
        json.dumps(
            {
                "verifications": [
                    {
                        "source_row_number": 3,
                        "court_slug": "review-court",
                        "address_index": 1,
                        "status": "review_required",
                        "message": "No unique high-confidence OS match was found",
                    }
                ]
            }
        )
    )
    (staging / "import_summary.json").write_text(json.dumps(summary))
    (staging / "api_readiness_report.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "court_slug": "example-court",
                        "source_row_numbers": [2],
                        "actions": [
                            {
                                "action_id": "example-court-1",
                                "resource": "building_facilities",
                                "method": "POST",
                                "readiness": "ready",
                                "path": "/courts/id/v1/building-facilities",
                                "body": {
                                    "parking": True,
                                    "freeWaterDispensers": False,
                                    "snackVendingMachines": False,
                                    "drinkVendingMachines": False,
                                    "cafeteria": False,
                                    "waitingArea": False,
                                    "quietRoom": False,
                                    "babyChanging": False,
                                    "wifi": False,
                                },
                            }
                        ],
                    }
                ]
            }
        )
    )
    (staging / "fact_import_payload.json").write_text(json.dumps({"records": []}))
    (staging / "nsu_cleaned_review.xlsx").write_bytes(b"review")
    (staging / "duplicate_forms_review.xlsx").write_bytes(b"duplicates")
    publish_run_archive(output_root, staging, run_id, "forms.csv", summary)
    return output_root, run_id


class _FakeExecutionClient:
    def __init__(self):
        self.writes = []

    def lookup_court(self, slug):
        return CourtReference("id", slug)

    def get(self, path):
        return ApiResponse(204)

    def write(self, method, path, body):
        self.writes.append((method, path, body))
        return ApiResponse(201)


class _FailingExecutionService:
    def get_ledger(self, run_id):
        return ExecutionLedger(run_id=run_id)

    def get_execution_summary(self, run_id):
        return {
            "selected_court_count": 0,
            "court_status_counts": {"completed": 0, "attention_required": 0},
            "action_status_counts": {"succeeded": 0, "blocked": 0, "failed": 0, "unknown": 0},
            "common_error_themes": [],
            "attention_by_request_type": [],
            "attention_actions": [],
            "courts": [],
        }

    def check_court(self, run_id, court_slug):
        raise ValueError("cannot check")

    def execute_action(self, run_id, court_slug, action_id):
        raise ValueError("cannot execute")

    def execute_safe_court_actions(self, run_id, court_slug):
        raise ValueError("cannot execute")

    def execute_all_safe_actions(self, run_id):
        raise ValueError("cannot execute")
