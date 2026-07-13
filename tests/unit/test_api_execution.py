import json

import httpx
import pytest

from fact_form_importer.config import AppConfig
from fact_form_importer.execution.fact_api import ApiResponse, FactApiExecutionClient
from fact_form_importer.execution.report import _error_theme
from fact_form_importer.execution.service import ApiExecutionService
from fact_form_importer.output.archive import publish_run_archive, stage_path
from fact_form_importer.validators.fact_api_courts import CourtReference


@pytest.fixture(autouse=True)
def audited_user_id(monkeypatch):
    monkeypatch.setenv("FACT_DATA_API_USER_ID", "00000000-0000-4000-a000-000000000001")


class FakeFactApiClient:
    def __init__(
        self,
        court="default",
        target=ApiResponse(204),
        write=ApiResponse(201),
        lookup_error=None,
        address_lookup=None,
    ):
        self.court = CourtReference("court-id", "example-court") if court == "default" else court
        self.target = target
        self.write_response = write
        self.lookup_error = lookup_error
        self.address_lookup = address_lookup
        self.get_paths = []
        self.writes = []
        self.lookup_slugs = []

    def lookup_court(self, slug):
        self.lookup_slugs.append(slug)
        if self.lookup_error:
            raise self.lookup_error
        return self.court

    def get(self, path):
        self.get_paths.append(path)
        if path.startswith("/search/address/v1/postcode/"):
            return self.address_lookup or ApiResponse(200, {"results": [{}]})
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


def test_execute_action_requires_an_audit_user_id(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    monkeypatch.delenv("FACT_DATA_API_USER_ID", raising=False)
    client = FakeFactApiClient()

    with pytest.raises(ValueError, match="FACT_DATA_API_USER_ID"):
        ApiExecutionService(tmp_path / "out", AppConfig(), client).execute_action(
            run_id, "example-court", "example-court-1"
        )

    assert client.writes == []


def test_preflight_blocks_missing_court_and_preserves_archive(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    archive_report = tmp_path / "out" / "final" / run_id / "api_readiness_report.json"
    original_report = archive_report.read_text()
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "false")
    client = FakeFactApiClient(court=None)

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(run_id, "example-court")

    assert ledger.courts["example-court"].actions["example-court-1"].status == "blocked"
    assert ledger.courts["example-court"].status == "attention_required"
    assert archive_report.read_text() == original_report


def test_preflight_records_status_and_token_guidance_when_court_lookup_is_rejected(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    request = httpx.Request("GET", "http://fact.example.test/courts/slug/example-court/v1")
    response = httpx.Response(401, request=request)
    lookup_error = httpx.HTTPStatusError(
        "Client error '401 Unauthorized'", request=request, response=response
    )
    client = FakeFactApiClient(lookup_error=lookup_error)

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(
        run_id, "example-court"
    )

    action = ledger.courts["example-court"].actions["example-court-1"]
    assert action.status == "unknown"
    assert action.last_response_status == 401
    assert action.reason == (
        "FaCT API rejected the court lookup (HTTP 401). "
        "Refresh FACT_DATA_API_BEARER_TOKEN and restart the importer UI."
    )
    assert ledger.courts["example-court"].status == "attention_required"


def test_preflight_records_connection_guidance_when_fact_api_is_unavailable(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    request = httpx.Request("GET", "http://fact.example.test/courts/slug/example-court/v1")
    client = FakeFactApiClient(lookup_error=httpx.ConnectError("connection refused", request=request))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(
        run_id, "example-court"
    )

    action = ledger.courts["example-court"].actions["example-court-1"]
    assert action.status == "unknown"
    assert action.last_response_status is None
    assert "Could not connect to FaCT API" in action.reason
    assert ledger.courts["example-court"].status == "attention_required"


def test_transient_lookup_failure_does_not_erase_a_confirmed_success(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(204), write=ApiResponse(201))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    service.execute_action(run_id, "example-court", "example-court-1")
    request = httpx.Request("GET", "http://fact.example.test/courts/slug/example-court/v1")
    client.lookup_error = httpx.ConnectError("connection refused", request=request)
    ledger = service.check_court(run_id, "example-court")

    action = ledger.courts["example-court"].actions["example-court-1"]
    assert action.status == "succeeded"
    assert [attempt.outcome for attempt in action.attempts] == ["ready", "succeeded"]
    assert ledger.courts["example-court"].status == "completed"


def test_execution_client_uses_existing_court_routes_and_bearer_token(monkeypatch):
    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact.example.test")
    monkeypatch.setenv("FACT_DATA_API_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("FACT_DATA_API_USER_ID", "00000000-0000-4000-a000-000000000001")
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
    assert "X-User-Id" not in requests[0].headers
    assert requests[-1].headers["X-User-Id"] == "00000000-0000-4000-a000-000000000001"
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


def test_execution_client_rejects_write_without_audited_user_id(monkeypatch):
    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact.example.test")
    monkeypatch.setenv("FACT_DATA_API_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("FACT_DATA_API_USER_ID", raising=False)
    http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(201)))

    with pytest.raises(ValueError, match="FACT_DATA_API_USER_ID"):
        FactApiExecutionClient(AppConfig(), http_client).write(
            "POST", "/courts/court-id/v1/address", {}
        )


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


def test_preflight_blocks_address_when_fact_os_lookup_rejects_postcode(tmp_path, monkeypatch):
    run_id = _archive(tmp_path, actions=[_address_action("example-court-1")])
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(
        target=ApiResponse(204),
        address_lookup=ApiResponse(400, {"message": "No address results returned from OS"}),
    )

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).execute_action(
        run_id, "example-court", "example-court-1"
    )

    state = ledger.courts["example-court"].actions["example-court-1"]
    assert state.status == "blocked"
    assert "Ordnance Survey postcode lookup failed" in state.reason
    assert client.writes == []
    assert client.get_paths == ["/search/address/v1/postcode/SW1A%201AA"]


def test_execution_normalises_safe_address_notation_before_writing(tmp_path, monkeypatch):
    run_id = _archive(
        tmp_path,
        actions=[
            _address_action(
                "example-court-1",
                address_line_1="Court C/o Service & Support",
            )
        ],
    )
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(204), write=ApiResponse(201))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).execute_action(
        run_id, "example-court", "example-court-1"
    )

    assert ledger.courts["example-court"].actions["example-court-1"].status == "succeeded"
    assert client.writes[0][2]["addressLine1"] == "Court care of Service and Support"
    assert client.get_paths == [
        "/search/address/v1/postcode/SW1A%201AA",
        "/courts/court-id/v1/address",
    ]


def test_preflight_reuses_verified_address_evidence_without_another_os_lookup(tmp_path, monkeypatch):
    action = _address_action("example-court-1")
    action["address_verification"] = {"status": "verified", "selected_candidate": {"uprn": "uprn-1"}}
    run_id = _archive(tmp_path, actions=[action])
    client = FakeFactApiClient(target=ApiResponse(204))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(run_id, "example-court")

    assert ledger.courts["example-court"].actions["example-court-1"].status == "ready"
    assert client.get_paths == ["/courts/court-id/v1/address"]


def test_preflight_caches_same_postcode_and_marks_rate_limiting_as_retriable(tmp_path, monkeypatch):
    actions = [_address_action("example-court-1"), _address_action("example-court-2")]
    run_id = _archive(tmp_path, actions=actions)
    client = FakeFactApiClient(target=ApiResponse(204))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(run_id, "example-court")

    assert all(action.status == "ready" for action in ledger.courts["example-court"].actions.values())
    assert client.get_paths.count("/search/address/v1/postcode/SW1A%201AA") == 1

    rate_limited_run = _archive(
        tmp_path / "rate-limited",
        actions=[_address_action("example-court-1")],
    )
    rate_limited_client = FakeFactApiClient(
        target=ApiResponse(204),
        address_lookup=ApiResponse(429, {"message": "Too many requests"}),
    )
    rate_limited = ApiExecutionService(
        tmp_path / "rate-limited" / "out", AppConfig(), rate_limited_client
    ).check_court(rate_limited_run, "example-court")

    action = rate_limited.courts["example-court"].actions["example-court-1"]
    assert action.status == "unknown"
    assert action.last_response_status == 429
    assert "rate-limited" in action.reason


def test_execution_reports_nested_api_validation_feedback(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(
        target=ApiResponse(204),
        write=ApiResponse(400, {"errors": {"courtId": "must not be null"}}),
    )

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).execute_action(
        run_id, "example-court", "example-court-1"
    )

    action = ledger.courts["example-court"].actions["example-court-1"]
    assert action.status == "failed"
    assert action.reason == "FaCT API rejected the write request (HTTP 400): courtId: must not be null"


def test_execute_all_safe_actions_runs_in_slug_order_continues_after_failure_and_writes_summary(
    tmp_path, monkeypatch
):
    run_id = _archive(
        tmp_path,
        records=[
            {
                "court_slug": "bravo-court",
                "court_id": "court-id",
                "source_row_numbers": [3],
                "actions": [_action("bravo-court-1")],
            },
            {
                "court_slug": "alpha-court",
                "court_id": "court-id",
                "source_row_numbers": [2],
                "actions": [_action("alpha-court-1")],
            },
        ],
    )
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")

    class FailFirstWriteClient(FakeFactApiClient):
        def write(self, method, path, body):
            self.writes.append((method, path, body))
            return ApiResponse(400, {"message": "first action rejected"}) if len(self.writes) == 1 else ApiResponse(201)

    client = FailFirstWriteClient(target=ApiResponse(204))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    ledger = service.execute_all_safe_actions(run_id)
    summary = service.get_execution_summary(run_id)

    assert client.lookup_slugs == ["alpha-court", "bravo-court"]
    assert len(client.writes) == 2
    assert ledger.courts["alpha-court"].status == "attention_required"
    assert ledger.courts["bravo-court"].status == "completed"
    assert summary["action_status_counts"]["failed"] == 1
    assert summary["action_status_counts"]["succeeded"] == 1
    assert summary["common_error_themes"][0]["code"] == "api_validation"
    assert (tmp_path / "out" / "execution-state" / f"{run_id}.summary.json").exists()
    assert (tmp_path / "out" / "execution_summary.json").exists()

    service.execute_all_safe_actions(run_id)
    assert len(client.writes) == 2


def test_execute_all_safe_actions_marks_unexpected_court_error_and_continues(tmp_path, monkeypatch):
    run_id = _archive(
        tmp_path,
        records=[
            {
                "court_slug": "alpha-court",
                "court_id": "court-id",
                "source_row_numbers": [2],
                "actions": [_action("alpha-court-1")],
            },
            {
                "court_slug": "bravo-court",
                "court_id": "court-id",
                "source_row_numbers": [3],
                "actions": [_action("bravo-court-1")],
            },
        ],
    )
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")

    class ExplodingTargetClient(FakeFactApiClient):
        def __init__(self):
            super().__init__(target=ApiResponse(204), write=ApiResponse(201))
            self.current_slug = None

        def lookup_court(self, slug):
            self.current_slug = slug
            return super().lookup_court(slug)

        def get(self, path):
            if self.current_slug == "alpha-court":
                raise RuntimeError("unexpected target failure")
            return super().get(path)

    client = ExplodingTargetClient()
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    ledger = service.execute_all_safe_actions(run_id)

    assert ledger.courts["alpha-court"].actions["alpha-court-1"].status == "unknown"
    assert ledger.courts["bravo-court"].actions["bravo-court-1"].status == "succeeded"
    assert "Unexpected execution error" in ledger.courts["alpha-court"].actions[
        "alpha-court-1"
    ].reason
    assert len(client.writes) == 1


@pytest.mark.parametrize(
    ("reason", "expected_code"),
    [
        ("liftDoorWidth is required by the FaCT API when lift is true", "missing_accessibility_detail"),
        (
            "professionalInformation.interviewRoomCount must be between 1 and 150 when interviewRooms is true",
            "invalid_interview_room_detail",
        ),
        (
            "openingTimesDetails must contain at least one valid opening period for the FaCT API",
            "invalid_opening_hours",
        ),
        ("phoneNumber does not match the FaCT API phone format", "invalid_contact_detail"),
        (
            "Address verification requires review: Postcode region is not supported by the FaCT API",
            "address_verification",
        ),
    ],
)
def test_execution_summary_groups_common_form_and_api_contract_gaps(reason, expected_code):
    code, _ = _error_theme("blocked", None, reason)

    assert code == expected_code


def _archive(tmp_path, actions=None, records=None):
    run_id = "20260713T120000Z-execution"
    output_root = tmp_path / "out"
    staging = stage_path(output_root, run_id)
    staging.mkdir(parents=True)
    report = {
        "run_id": run_id,
        "records": records
        or [
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


def _address_action(action_id, address_line_1="1 Main Street"):
    return {
        "action_id": action_id,
        "resource": "address",
        "method": "POST",
        "path": "/courts/court-id/v1/address",
        "readiness": "ready",
        "body": {
            "addressLine1": address_line_1,
            "townCity": "London",
            "postcode": "SW1A 1AA",
            "addressType": "VISIT_US",
        },
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
