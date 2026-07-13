import json

import httpx

from fact_form_importer.config import AppConfig
from fact_form_importer.execution.fact_api import ApiResponse, FactApiExecutionClient
from fact_form_importer.execution.service import ApiExecutionService
from fact_form_importer.output.archive import publish_run_archive, stage_path
from fact_form_importer.validators.fact_api_courts import CourtReference


class FakeFactApiClient:
    def __init__(self, court="default", target=ApiResponse(204), write=ApiResponse(201)):
        self.court = CourtReference("court-id", "example-court") if court == "default" else court
        self.target = target
        self.write_response = write
        self.get_paths = []
        self.writes = []

    def lookup_court(self, slug):
        return self.court

    def get(self, path):
        self.get_paths.append(path)
        return self.target

    def write(self, method, path, body):
        self.writes.append((method, path, body))
        return self.write_response


def test_check_court_blocks_an_existing_target_section(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "false")
    client = FakeFactApiClient(target=ApiResponse(200, {"parking": True}))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(run_id, "example-court")

    action = ledger.courts["example-court"].actions["example-court-1"]
    assert action.status == "blocked"
    assert "already contains" in action.reason
    assert ledger.courts["example-court"].status == "attention_required"
    assert client.writes == []


def test_execute_action_writes_only_after_safe_preflight_and_persists_ledger(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(204), write=ApiResponse(201))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    ledger = service.execute_action(run_id, "example-court", "example-court-1")

    assert client.get_paths == ["/courts/court-id/v1/building-facilities"]
    assert client.writes == [
        ("POST", "/courts/court-id/v1/building-facilities", _building_facilities_body("court-id"))
    ]
    assert ledger.courts["example-court"].actions["example-court-1"].status == "succeeded"
    assert ledger.courts["example-court"].status == "completed"
    assert (tmp_path / "out" / "execution-state" / f"{run_id}.json").exists()


def test_execute_action_requires_write_circuit_breaker(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "false")
    service = ApiExecutionService(tmp_path / "out", AppConfig(), FakeFactApiClient())

    try:
        service.execute_action(run_id, "example-court", "example-court-1")
    except ValueError as exc:
        assert "writes are disabled" in str(exc)
    else:
        raise AssertionError("Expected circuit breaker to prevent write")


def test_preflight_blocks_missing_court_and_preserves_archive(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    archive_report = tmp_path / "out" / "final" / run_id / "api_readiness_report.json"
    original_report = archive_report.read_text()
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "false")
    client = FakeFactApiClient(court=None)

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(run_id, "example-court")

    assert ledger.courts["example-court"].actions["example-court-1"].status == "blocked"
    assert archive_report.read_text() == original_report


def test_execution_client_uses_existing_court_routes_and_bearer_token(monkeypatch):
    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact.example.test")
    monkeypatch.setenv("FACT_DATA_API_BEARER_TOKEN", "test-token")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/courts/slug/example-court/v1":
            return httpx.Response(200, json={"id": "court-id", "slug": "example-court", "name": "Example"})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": "created"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = FactApiExecutionClient(AppConfig(), http_client)

    court = client.lookup_court("example-court")
    fetched = client.get("/courts/court-id/v1/address")
    written = client.write("POST", "/courts/court-id/v1/address", {"addressLine1": "1 Main Street"})
    client.close()

    assert court == CourtReference("court-id", "example-court", "Example")
    assert fetched.status_code == 200 and fetched.body == []
    assert written.status_code == 201 and written.body == {"id": "created"}
    assert all(request.headers["Authorization"] == "Bearer test-token" for request in requests)
    assert json.loads(requests[-1].content) == {"addressLine1": "1 Main Street"}


def test_execution_client_handles_missing_court_and_rejects_missing_config(monkeypatch):
    monkeypatch.delenv("FACT_DATA_API_BASE_URL", raising=False)
    monkeypatch.delenv("FACT_DATA_API_BEARER_TOKEN", raising=False)
    try:
        FactApiExecutionClient(AppConfig())
    except ValueError as exc:
        assert "BASE_URL" in str(exc)
    else:
        raise AssertionError("Expected configuration validation")

    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact.example.test")
    monkeypatch.setenv("FACT_DATA_API_BEARER_TOKEN", "test-token")
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404, request=request))
    )
    assert FactApiExecutionClient(AppConfig(), http_client).lookup_court("missing-court") is None


def test_execute_safe_actions_marks_failed_and_pending_actions_as_attention(tmp_path, monkeypatch):
    run_id = _archive(tmp_path, actions=[
        _action("example-court-1"),
        {**_action("example-court-2"), "readiness": "pending", "reason": "invalid API body"},
    ])
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(204), write=ApiResponse(400, {"message": "bad"}))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).execute_safe_court_actions(
        run_id, "example-court"
    )

    states = ledger.courts["example-court"].actions
    assert states["example-court-1"].status == "failed"
    assert states["example-court-2"].status == "blocked"
    assert ledger.courts["example-court"].status == "attention_required"
    assert len(client.writes) == 1


def test_preflight_blocks_historic_action_body_that_no_longer_meets_api_contract(tmp_path, monkeypatch):
    run_id = _archive(tmp_path, actions=[_action("example-court-1", body={"parking": True})])
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(204))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).execute_action(
        run_id, "example-court", "example-court-1"
    )

    state = ledger.courts["example-court"].actions["example-court-1"]
    assert state.status == "blocked"
    assert "freeWaterDispensers" in state.reason
    assert client.writes == []


def test_execution_records_safe_field_feedback_from_an_unexpected_api_400(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(
        target=ApiResponse(204),
        write=ApiResponse(400, {"courtId": "must not be null", "timestamp": "ignored"}),
    )

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).execute_action(
        run_id, "example-court", "example-court-1"
    )

    state = ledger.courts["example-court"].actions["example-court-1"]
    assert state.status == "failed"
    assert state.last_response_status == 400
    assert state.reason == "FaCT API rejected the write request (HTTP 400): courtId: must not be null"


def _archive(tmp_path, actions=None):
    run_id = "20260713T120000Z-execution"
    output_root = tmp_path / "out"
    staging = stage_path(output_root, run_id)
    staging.mkdir(parents=True)
    report = {
        "run_id": run_id,
        "records": [
            {
                "court_slug": "example-court",
                "court_id": "court-id",
                "source_row_numbers": [2],
                "actions": actions or [_action("example-court-1")],
            }
        ],
    }
    (staging / "api_readiness_report.json").write_text(json.dumps(report))
    publish_run_archive(output_root, staging, run_id, "forms.csv", {})
    return run_id


def _action(action_id, body=None):
    return {
        "action_id": action_id,
        "resource": "building_facilities",
        "method": "POST",
        "path": "/courts/court-id/v1/building-facilities",
        "readiness": "ready",
        "body": _building_facilities_body() if body is None else body,
    }


def _building_facilities_body(court_id=None):
    body = {
        "parking": True,
        "freeWaterDispensers": False,
        "snackVendingMachines": False,
        "drinkVendingMachines": False,
        "cafeteria": False,
        "waitingArea": False,
        "quietRoom": False,
        "babyChanging": False,
        "wifi": False,
    }
    if court_id:
        body["courtId"] = court_id
    return body
