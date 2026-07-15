import json

import httpx
import pytest

from fact_form_importer.config import AppConfig
from fact_form_importer.execution.fact_api import ApiResponse, FactApiExecutionClient
from fact_form_importer.execution.models import ActionExecutionState, CourtExecutionState
from fact_form_importer.execution.report import (
    EXECUTION_SUMMARY_VERSION,
    _error_theme,
    _group_attention_by_request_type,
)
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


def test_check_court_requires_approval_for_an_existing_target_section(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "false")
    client = FakeFactApiClient(target=ApiResponse(200, {"parking": True}))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(
        run_id, "example-court"
    )

    action = ledger.courts["example-court"].actions["example-court-1"]
    assert action.status == "awaiting_approval"
    assert "effective after values" in action.reason
    assert ledger.courts["example-court"].status == "awaiting_approval"
    assert client.writes == []


def test_exact_target_replacement_approval_is_separate_and_hash_bound(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(200, {"parking": False}))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    comparison = service.refresh_target_comparison(
        run_id, "example-court", "example-court-1"
    )
    service.approve_target_change(run_id, comparison.change_id)

    assert client.writes == []
    completed = service.execute_action(run_id, "example-court", "example-court-1")
    assert completed.courts["example-court"].actions["example-court-1"].status == "succeeded"
    assert client.writes

    changed_root = tmp_path / "changed"
    changed_run = _archive(changed_root)
    changed_client = FakeFactApiClient(target=ApiResponse(200, {"parking": False}))
    changed_service = ApiExecutionService(changed_root / "out", AppConfig(), changed_client)
    changed_comparison = changed_service.refresh_target_comparison(
        changed_run, "example-court", "example-court-1"
    )
    changed_service.approve_target_change(changed_run, changed_comparison.change_id)
    changed_client.target = ApiResponse(200, {"parking": None})

    held = changed_service.execute_action(
        changed_run, "example-court", "example-court-1"
    )

    assert held.courts["example-court"].actions["example-court-1"].status == "awaiting_approval"
    assert changed_client.writes == []


def test_collection_merge_updates_and_creates_without_deleting_unmatched_entries(
    tmp_path, monkeypatch
):
    contact_type = "00000000-0000-0000-0000-000000000001"
    new_type = "00000000-0000-0000-0000-000000000002"
    old_type = "00000000-0000-0000-0000-000000000003"
    proposed = [
        {
            "courtId": "court-id",
            "courtContactDescriptionId": contact_type,
            "explanation": "Updated",
        },
        {
            "courtId": "court-id",
            "courtContactDescriptionId": new_type,
            "phoneNumber": "020 7000 0000",
        },
    ]
    action = {
        "action_id": "example-contact-section",
        "resource": "contact_detail",
        "method": "POST",
        "path": "/courts/court-id/v1/contact-details",
        "readiness": "ready",
        "body": proposed[0],
        "proposed_items": proposed,
    }
    run_id = _archive(tmp_path, actions=[action])
    current = [
        {
            "id": "current-contact",
            "courtContactDescriptionId": contact_type,
            "explanation": "Old",
        },
        {
            "id": "surplus-contact",
            "courtContactDescriptionId": old_type,
            "explanation": "Remove",
        },
    ]
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(200, current))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)
    comparison = service.refresh_target_comparison(
        run_id, "example-court", "example-contact-section"
    )
    service.approve_target_change(run_id, comparison.change_id)

    completed = service.execute_action(
        run_id, "example-court", "example-contact-section"
    )

    assert completed.courts["example-court"].actions[
        "example-contact-section"
    ].status == "succeeded"
    assert [method for method, _, _ in client.writes] == ["PUT", "POST"]
    assert all(not path.endswith("/surplus-contact") for _, path, _ in client.writes)


def test_partial_collection_failure_stops_deletion_and_refreshes_live_state(
    tmp_path, monkeypatch
):
    contact_type = "00000000-0000-0000-0000-000000000001"
    new_type = "00000000-0000-0000-0000-000000000002"
    old_type = "00000000-0000-0000-0000-000000000003"
    proposed = [
        {
            "courtId": "court-id",
            "courtContactDescriptionId": contact_type,
            "explanation": "Updated",
        },
        {
            "courtId": "court-id",
            "courtContactDescriptionId": new_type,
            "phoneNumber": "020 7000 0000",
        },
    ]
    action = {
        "action_id": "example-contact-section",
        "resource": "contact_detail",
        "method": "POST",
        "path": "/courts/court-id/v1/contact-details",
        "readiness": "ready",
        "body": proposed[0],
        "proposed_items": proposed,
    }
    run_id = _archive(tmp_path, actions=[action])
    current = [
        {
            "id": "current-contact",
            "courtContactDescriptionId": contact_type,
            "explanation": "Old",
        },
        {
            "id": "surplus-contact",
            "courtContactDescriptionId": old_type,
            "explanation": "Remove",
        },
    ]

    class PartialFailureClient(FakeFactApiClient):
        def write(self, method, path, body):
            self.writes.append((method, path, body))
            if len(self.writes) == 1:
                self.target = ApiResponse(
                    200,
                    [
                        {"id": "current-contact", **proposed[0]},
                        current[1],
                    ],
                )
                return ApiResponse(200)
            return ApiResponse(400, {"message": "create rejected"})

    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = PartialFailureClient(target=ApiResponse(200, current))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)
    comparison = service.refresh_target_comparison(
        run_id, "example-court", "example-contact-section"
    )
    service.approve_target_change(run_id, comparison.change_id)

    failed = service.execute_action(run_id, "example-court", "example-contact-section")
    state = failed.courts["example-court"].actions["example-contact-section"]

    assert state.status == "failed"
    assert [method for method, _, _ in client.writes] == ["PUT", "POST"]
    assert "re-read" in state.reason
    assert comparison.change_id not in service.get_execution_review(run_id).target_approvals


def test_refresh_all_comparisons_skips_unselected_duplicates_and_reports_failures(tmp_path):
    normal = _action("normal-action")
    duplicate = {
        **_action("duplicate-action"),
        "source_selection_required": True,
        "source_row_number": 3,
    }
    run_id = _archive(
        tmp_path,
        records=[
            {
                "court_slug": "example-court",
                "court_id": "court-id",
                "source_row_numbers": [2],
                "actions": [normal],
            },
            {
                "court_slug": "duplicate-court",
                "court_id": "duplicate-id",
                "source_row_numbers": [3, 4],
                "actions": [duplicate],
            },
        ],
    )
    service = ApiExecutionService(
        tmp_path / "out", AppConfig(), FakeFactApiClient(target=ApiResponse(204))
    )

    service.refresh_all_target_comparisons(run_id)

    assert len(service.get_execution_review(run_id).comparisons) == 1

    failing_root = tmp_path / "failing"
    failing_run = _archive(failing_root)
    failing = ApiExecutionService(
        failing_root / "out", AppConfig(), FakeFactApiClient(target=ApiResponse(500))
    )
    with pytest.raises(ValueError, match="1 court error"):
        failing.refresh_all_target_comparisons(failing_run)

    request = httpx.Request("GET", "http://fact.test/courts/slug/example-court/v1")
    response = httpx.Response(401, request=request)
    authentication_error = httpx.HTTPStatusError(
        "Client error '401 Unauthorized'", request=request, response=response
    )
    authentication = ApiExecutionService(
        failing_root / "out",
        AppConfig(),
        FakeFactApiClient(lookup_error=authentication_error),
    )
    with pytest.raises(ValueError, match="Refresh FACT_DATA_API_BEARER_TOKEN"):
        authentication.refresh_all_target_comparisons(failing_run)


def test_preflight_blocks_changed_court_uuid_and_marks_unexpected_target_unknown(tmp_path):
    changed_run = _archive(
        tmp_path,
        records=[
            {
                "court_slug": "example-court",
                "court_id": "reviewed-old-id",
                "source_row_numbers": [2],
                "actions": [_action("changed-court-action")],
            }
        ],
    )
    changed_service = ApiExecutionService(
        tmp_path / "out", AppConfig(), FakeFactApiClient()
    )

    changed = changed_service.check_court(changed_run, "example-court")

    assert changed.courts["example-court"].actions[
        "changed-court-action"
    ].status == "blocked"
    assert "UUID no longer matches" in changed.courts["example-court"].actions[
        "changed-court-action"
    ].reason

    unknown_root = tmp_path / "unknown"
    unknown_run = _archive(unknown_root)
    unknown_service = ApiExecutionService(
        unknown_root / "out", AppConfig(), FakeFactApiClient(target=ApiResponse(500))
    )

    unknown = unknown_service.check_court(unknown_run, "example-court")

    assert unknown.courts["example-court"].actions["example-court-1"].status == "unknown"
    assert "unexpected response" in unknown.courts["example-court"].actions[
        "example-court-1"
    ].reason


def test_duplicate_preflight_checks_only_the_selected_source_proposal(tmp_path):
    actions = [
        {
            **_action("row-2-action"),
            "source_selection_required": True,
            "source_row_number": 2,
        },
        {
            **_action("row-3-action"),
            "source_selection_required": True,
            "source_row_number": 3,
        },
    ]
    run_id = _archive(
        tmp_path,
        records=[
            {
                "court_slug": "example-court",
                "court_id": "court-id",
                "source_row_numbers": [2, 3],
                "actions": actions,
            }
        ],
    )
    service = ApiExecutionService(
        tmp_path / "out", AppConfig(), FakeFactApiClient(target=ApiResponse(204))
    )

    waiting = service.check_court(run_id, "example-court")
    assert waiting.courts["example-court"].actions["row-2-action"].status == "awaiting_approval"
    service.select_source_row(run_id, "example-court", 2)
    checked = service.check_court(run_id, "example-court")

    assert checked.courts["example-court"].actions["row-2-action"].status == "ready"
    assert checked.courts["example-court"].actions["row-3-action"].status == "awaiting_approval"


def test_execute_action_writes_after_safe_preflight_without_local_approval(tmp_path, monkeypatch):
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


def test_llm_dependent_action_waits_for_every_approval_before_execution(tmp_path, monkeypatch):
    action = _action("example-court-1")
    action["llm_review_ids"] = ["review-1", "review-2"]
    run_id = _archive(tmp_path, actions=[action])
    _write_llm_review(tmp_path, run_id, ["review-1", "review-2"])
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(204), write=ApiResponse(201))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    waiting = service.execute_action(run_id, "example-court", "example-court-1")

    assert waiting.courts["example-court"].actions["example-court-1"].status == "awaiting_approval"
    assert client.writes == []
    assert service.get_execution_summary(run_id)["action_status_counts"]["awaiting_approval"] == 1

    service.approve_llm_review(run_id, "review-1")
    still_waiting = service.execute_action(run_id, "example-court", "example-court-1")
    assert (
        still_waiting.courts["example-court"].actions["example-court-1"].status
        == "awaiting_approval"
    )
    assert client.writes == []

    service.approve_llm_review(run_id, "review-2")
    assert client.writes == []  # Approval is deliberately separate from execution.
    completed = service.execute_action(run_id, "example-court", "example-court-1")

    assert completed.courts["example-court"].actions["example-court-1"].status == "succeeded"
    assert client.writes
    summary = service.get_execution_summary(run_id)
    assert summary["llm_approval_counts"] == {
        "total": 2,
        "approved": 2,
        "manual_approved": 2,
        "auto_approved": 0,
        "auto_approved_total": 0,
        "auto_approved_addresses": 0,
        "auto_approved_unchanged_fields": 0,
        "auto_approved_fields": 0,
        "pending": 0,
        "already_executed": 0,
        "not_actionable": 0,
    }
    assert summary["court_status_counts"]["completed"] == 1


def test_approved_address_uses_os_mapping_and_requires_fresh_uprn(tmp_path, monkeypatch):
    action = _address_action("example-court-1", "Submitted Court")
    action["llm_review_ids"] = ["address-review"]
    run_id = _archive(tmp_path, actions=[action])
    _write_llm_review(
        tmp_path,
        run_id,
        ["address-review"],
        kind="address",
        api_body_patch={
            "addressLine1": "OS COURT",
            "addressLine2": "1 MAIN STREET",
            "townCity": "LONDON",
            "county": None,
            "postcode": "SW1A 1AA",
        },
        uprn="uprn-1",
    )
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(
        target=ApiResponse(204),
        address_lookup=ApiResponse(200, {"results": [{"DPA": {"UPRN": "uprn-1"}}]}),
    )
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)
    service.approve_llm_review(run_id, "address-review")

    ledger = service.execute_action(run_id, "example-court", "example-court-1")

    assert ledger.courts["example-court"].actions["example-court-1"].status == "succeeded"
    assert client.writes[0][2]["addressLine1"] == "OS COURT"
    assert client.writes[0][2]["addressLine2"] == "1 MAIN STREET"

    missing_run = _archive(tmp_path / "missing", actions=[action])
    _write_llm_review(
        tmp_path / "missing",
        missing_run,
        ["address-review"],
        kind="address",
        api_body_patch={"addressLine1": "OS COURT", "townCity": "LONDON", "postcode": "SW1A 1AA"},
        uprn="uprn-1",
    )
    missing_client = FakeFactApiClient(
        target=ApiResponse(204),
        address_lookup=ApiResponse(200, {"results": [{"DPA": {"UPRN": "different"}}]}),
    )
    missing_service = ApiExecutionService(tmp_path / "missing" / "out", AppConfig(), missing_client)
    missing_service.approve_llm_review(missing_run, "address-review")

    blocked = missing_service.execute_action(missing_run, "example-court", "example-court-1")

    assert blocked.courts["example-court"].actions["example-court-1"].status == "blocked"
    assert "no longer returned" in blocked.courts["example-court"].actions["example-court-1"].reason
    assert missing_client.writes == []


def test_reviewer_edited_address_is_stored_invalidates_comparison_and_is_executed(
    tmp_path, monkeypatch
):
    action = _address_action("example-court-1", "Submitted Court")
    action["llm_review_ids"] = ["address-review"]
    action["proposed_items"] = [dict(action["body"])]
    run_id = _archive(tmp_path, actions=[action])
    _write_llm_review(
        tmp_path,
        run_id,
        ["address-review"],
        kind="address",
        api_body_patch={
            "addressLine1": "OS Court",
            "addressLine2": None,
            "townCity": "London",
            "county": None,
            "postcode": "SW1A 1AA",
        },
        uprn="uprn-1",
        candidates=[{"uprn": "uprn-1"}, {"uprn": "uprn-2"}],
    )
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(
        target=ApiResponse(
            200,
            [
                {
                    "id": "address-id",
                    "addressLine1": "Existing Court",
                    "townCity": "London",
                    "postcode": "SW1A 1AA",
                    "addressType": "VISIT_US",
                }
            ],
        ),
        address_lookup=ApiResponse(200, {"results": [{"DPA": {"UPRN": "uprn-1"}}]}),
    )
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)
    service.approve_llm_review(run_id, "address-review")
    comparison = service.refresh_target_comparison(
        run_id, "example-court", "example-court-1"
    )
    service.approve_target_change(run_id, comparison.change_id)

    approvals = service.approve_llm_review(
        run_id,
        "address-review",
        address_patch={
            "addressLine1": "Reviewer Court",
            "addressLine2": "PO Box 12",
            "townCity": "London",
            "county": "Greater London",
            "postcode": "SW1A 1AA",
        },
    )

    approval = approvals.approvals["address-review"]
    assert approval.approval_method == "manual"
    assert approval.approved_address_patch["addressLine1"] == "Reviewer Court"
    assert approval.decision_history
    review_state = service.get_execution_review(run_id)
    assert comparison.change_id not in review_state.comparisons
    assert comparison.change_id not in review_state.target_approvals

    refreshed = service.refresh_target_comparison(
        run_id, "example-court", "example-court-1"
    )
    service.approve_target_change(run_id, refreshed.change_id)
    completed = service.execute_action(run_id, "example-court", "example-court-1")

    assert completed.courts["example-court"].actions["example-court-1"].status == "succeeded"
    assert client.writes[0][2]["addressLine1"] == "Reviewer Court"
    assert client.writes[0][2]["addressLine2"] == "PO Box 12"


def test_reviewer_address_validation_and_execution_state_restrictions(tmp_path):
    action = _address_action("example-court-1", "Submitted Court")
    action["llm_review_ids"] = ["address-review"]
    run_id = _archive(tmp_path, actions=[action])
    _write_llm_review(
        tmp_path,
        run_id,
        ["address-review"],
        kind="address",
        api_body_patch={
            "addressLine1": "OS Court",
            "townCity": "London",
            "postcode": "SW1A 1AA",
        },
        uprn="uprn-1",
        candidates=[{"uprn": "uprn-1"}, {"uprn": "uprn-2"}],
    )
    service = ApiExecutionService(tmp_path / "out", AppConfig(), FakeFactApiClient())

    with pytest.raises(ValueError, match="address line 1"):
        service.approve_llm_review(
            run_id,
            "address-review",
            address_patch={"addressLine1": "", "townCity": "London", "postcode": "SW1A 1AA"},
        )
    with pytest.raises(ValueError, match="active execution job"):
        service.approve_llm_review(
            run_id,
            "address-review",
            address_patch={
                "addressLine1": "Court",
                "townCity": "London",
                "postcode": "SW1A 1AA",
            },
            execution_job_active=True,
        )

    ledger = service.store.load(run_id)
    ledger.courts["example-court"] = CourtExecutionState(
        court_slug="example-court",
        actions={
            "example-court-1": ActionExecutionState(
                action_id="example-court-1", status="unknown"
            )
        },
    )
    service.store.save(ledger)
    with pytest.raises(ValueError, match="becomes uncertain"):
        service.approve_llm_review(
            run_id,
            "address-review",
            address_patch={
                "addressLine1": "Court",
                "townCity": "London",
                "postcode": "SW1A 1AA",
            },
        )


def test_strict_address_policy_approves_without_executing_and_retains_preflight(
    tmp_path, monkeypatch
):
    action = _address_action("example-court-1", "Submitted Court")
    action["llm_review_ids"] = ["address-review"]
    run_id = _archive(tmp_path, actions=[action])
    _write_llm_review(
        tmp_path,
        run_id,
        ["address-review"],
        kind="address",
        api_body_patch={
            "addressLine1": "OS COURT",
            "townCity": "LONDON",
            "postcode": "SW1A 1AA",
        },
        uprn="uprn-1",
        candidates=[{"uprn": "uprn-1"}],
    )
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(
        target=ApiResponse(204),
        address_lookup=ApiResponse(200, {"results": [{"DPA": {"UPRN": "uprn-1"}}]}),
    )
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    approvals = service.reconcile_automatic_approvals(run_id)

    assert approvals.approvals["address-review"].approval_method == "policy"
    assert client.writes == []
    assert service.get_execution_summary(run_id)["llm_approval_counts"]["auto_approved"] == 1

    completed = service.execute_action(run_id, "example-court", "example-court-1")

    assert completed.courts["example-court"].actions["example-court-1"].status == "succeeded"
    assert client.writes[0][2]["addressLine1"] == "OS COURT"


def test_legacy_succeeded_action_is_not_retroactively_held_for_approval(tmp_path):
    action = _action("example-court-1")
    action["llm_review_ids"] = ["legacy-review"]
    run_id = _archive(tmp_path, actions=[action])
    _write_llm_review(tmp_path, run_id, ["legacy-review"])
    service = ApiExecutionService(tmp_path / "out", AppConfig(), FakeFactApiClient())
    ledger = service.store.load(run_id)
    ledger.courts["example-court"] = CourtExecutionState(
        court_slug="example-court",
        court_id="court-id",
        status="completed",
        actions={
            "example-court-1": ActionExecutionState(action_id="example-court-1", status="succeeded")
        },
    )
    service.store.save(ledger)

    summary = service.get_execution_summary(run_id)
    review = service.get_llm_actions_review(run_id)

    assert summary["action_status_counts"]["succeeded"] == 1
    assert summary["llm_approval_counts"]["already_executed"] == 1
    assert summary["llm_approval_counts"]["pending"] == 0
    assert review["items"][0]["approval_status"] == "already_executed"
    with pytest.raises(ValueError, match="already used"):
        service.approve_llm_review(run_id, "legacy-review")


def test_legacy_po_box_only_approval_dependency_is_ignored(tmp_path, monkeypatch):
    action = _address_action("example-court-1", "PO Box 12")
    action["llm_review_ids"] = ["legacy-po-box-review"]
    run_id = _archive(tmp_path, actions=[action])
    _write_llm_review(
        tmp_path,
        run_id,
        ["legacy-po-box-review"],
        kind="address",
    )
    path = tmp_path / "out" / "final" / run_id / "llm_actions_review.json"
    report = json.loads(path.read_text())
    report["items"][0]["address_mode"] = "po_box"
    path.write_text(json.dumps(report))
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(204), write=ApiResponse(201))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    assert service.get_llm_actions_review(run_id)["items"] == []
    completed = service.execute_action(run_id, "example-court", "example-court-1")

    state = completed.courts["example-court"].actions["example-court-1"]
    assert state.status == "succeeded"
    assert client.writes[0][2]["addressLine1"] == "PO Box 12"


def test_execution_state_ignores_legacy_local_approval_fields():
    state = CourtExecutionState.model_validate(
        {
            "court_slug": "example-court",
            "approval_status": "approved",
            "approved_at": "2026-07-13T12:00:00Z",
        }
    )

    assert state.court_slug == "example-court"
    assert "approval_status" not in state.model_dump()


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

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(
        run_id, "example-court"
    )

    assert ledger.courts["example-court"].actions["example-court-1"].status == "blocked"
    assert ledger.courts["example-court"].status == "attention_required"
    assert archive_report.read_text() == original_report


def test_preflight_records_status_and_token_guidance_when_court_lookup_is_rejected(
    tmp_path, monkeypatch
):
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
    client = FakeFactApiClient(
        lookup_error=httpx.ConnectError("connection refused", request=request)
    )

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
            return httpx.Response(
                200, json={"id": "court-id", "slug": "example-court", "name": "Example"}
            )
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
    run_id = _archive(
        tmp_path,
        actions=[
            _action("example-court-1"),
            {**_action("example-court-2"), "readiness": "pending", "reason": "invalid API body"},
        ],
    )
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(204), write=ApiResponse(400, {"message": "bad"}))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    ledger = service.execute_safe_court_actions(run_id, "example-court")

    states = ledger.courts["example-court"].actions
    assert states["example-court-1"].status == "failed"
    assert states["example-court-2"].status == "blocked"
    assert ledger.courts["example-court"].status == "attention_required"
    assert len(client.writes) == 1


def test_preflight_blocks_historic_action_body_that_no_longer_meets_api_contract(
    tmp_path, monkeypatch
):
    run_id = _archive(tmp_path, actions=[_action("example-court-1", body={"parking": True})])
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(204))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    ledger = service.execute_action(run_id, "example-court", "example-court-1")

    state = ledger.courts["example-court"].actions["example-court-1"]
    assert state.status == "blocked"
    assert "freeWaterDispensers" in state.reason
    assert client.writes == []


def test_preflight_blocks_invalid_accessibility_phone_before_api_write(tmp_path, monkeypatch):
    run_id = _archive(
        tmp_path,
        actions=[
            {
                "action_id": "example-court-1",
                "resource": "accessibility_options",
                "method": "POST",
                "path": "/courts/court-id/v1/accessibility-options",
                "readiness": "ready",
                "body": {
                    "courtId": "court-id",
                    "accessibleParking": False,
                    "accessibleEntrance": False,
                    "accessibleEntrancePhoneNumber": "ask reception",
                    "hearingEnhancementEquipment": "HEARING_LOOP_SYSTEMS",
                    "lift": True,
                    "liftDoorWidth": 1,
                    "liftDoorLimit": 1,
                    "quietRoom": False,
                },
            }
        ],
    )
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(target=ApiResponse(204), write=ApiResponse(201))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).execute_action(
        run_id, "example-court", "example-court-1"
    )

    action = ledger.courts["example-court"].actions["example-court-1"]
    assert action.status == "blocked"
    assert "accessibleEntrancePhoneNumber does not match" in action.reason
    assert client.writes == []


def test_execution_records_safe_field_feedback_from_an_unexpected_api_400(tmp_path, monkeypatch):
    run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(
        target=ApiResponse(204),
        write=ApiResponse(400, {"courtId": "must not be null", "timestamp": "ignored"}),
    )
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    ledger = service.execute_action(run_id, "example-court", "example-court-1")

    state = ledger.courts["example-court"].actions["example-court-1"]
    assert state.status == "failed"
    assert state.last_response_status == 400
    assert (
        state.reason == "FaCT API rejected the write request (HTTP 400): courtId: must not be null"
    )


def test_preflight_blocks_address_when_fact_os_lookup_rejects_postcode(tmp_path, monkeypatch):
    run_id = _archive(tmp_path, actions=[_address_action("example-court-1")])
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    client = FakeFactApiClient(
        target=ApiResponse(204),
        address_lookup=ApiResponse(400, {"message": "No address results returned from OS"}),
    )
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    ledger = service.execute_action(run_id, "example-court", "example-court-1")

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
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    ledger = service.execute_action(run_id, "example-court", "example-court-1")

    assert ledger.courts["example-court"].actions["example-court-1"].status == "succeeded"
    assert client.writes[0][2]["addressLine1"] == "Court care of Service and Support"
    assert client.get_paths == [
        "/search/address/v1/postcode/SW1A%201AA",
        "/courts/court-id/v1/address",
    ]


def test_preflight_reuses_verified_address_evidence_without_another_os_lookup(
    tmp_path, monkeypatch
):
    action = _address_action("example-court-1")
    action["address_verification"] = {
        "status": "verified",
        "selected_candidate": {"uprn": "uprn-1"},
    }
    run_id = _archive(tmp_path, actions=[action])
    client = FakeFactApiClient(target=ApiResponse(204))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(
        run_id, "example-court"
    )

    assert ledger.courts["example-court"].actions["example-court-1"].status == "ready"
    assert client.get_paths == ["/courts/court-id/v1/address"]


def test_preflight_caches_same_postcode_and_marks_rate_limiting_as_retriable(tmp_path, monkeypatch):
    actions = [_address_action("example-court-1"), _address_action("example-court-2")]
    run_id = _archive(tmp_path, actions=actions)
    client = FakeFactApiClient(target=ApiResponse(204))

    ledger = ApiExecutionService(tmp_path / "out", AppConfig(), client).check_court(
        run_id, "example-court"
    )

    assert all(
        action.status == "ready" for action in ledger.courts["example-court"].actions.values()
    )
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
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    ledger = service.execute_action(run_id, "example-court", "example-court-1")

    action = ledger.courts["example-court"].actions["example-court-1"]
    assert action.status == "failed"
    assert (
        action.reason == "FaCT API rejected the write request (HTTP 400): courtId: must not be null"
    )


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
            return (
                ApiResponse(400, {"message": "first action rejected"})
                if len(self.writes) == 1
                else ApiResponse(201)
            )

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
    assert summary["summary_version"] == EXECUTION_SUMMARY_VERSION
    assert summary["attention_by_request_type"][0]["resource"] == "building_facilities"
    assert (
        summary["attention_by_request_type"][0]["outcomes"][0]["classification"] == "api_rejection"
    )
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
    assert (
        "Unexpected execution error" in ledger.courts["alpha-court"].actions["alpha-court-1"].reason
    )
    assert len(client.writes) == 1


def test_execute_all_safe_actions_runs_courts_without_local_approval(tmp_path, monkeypatch):
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
    client = FakeFactApiClient(target=ApiResponse(204), write=ApiResponse(201))
    service = ApiExecutionService(tmp_path / "out", AppConfig(), client)

    ledger = service.execute_all_safe_actions(run_id)
    summary = service.get_execution_summary(run_id)

    assert client.lookup_slugs == ["alpha-court", "bravo-court"]
    assert len(client.writes) == 2
    assert {court.status for court in ledger.courts.values()} == {"completed"}
    assert summary["action_status_counts"]["succeeded"] == 2


@pytest.mark.parametrize(
    ("reason", "expected_code"),
    [
        (
            "liftDoorWidth is required by the FaCT API when lift is true",
            "missing_accessibility_detail",
        ),
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


def test_execution_summary_groups_distinct_reasons_by_request_type():
    actions = [
        {
            "court_slug": "alpha-court",
            "resource": "address",
            "method": "POST",
            "path": "/courts/00000000-0000-4000-a000-000000000001/v1/address",
            "status": "blocked",
            "http_status": None,
            "reason": (
                "Address verification requires review: FaCT/Ordnance Survey returned no "
                "address result: No address results returned from OS for postcode AA1 1AA"
            ),
        },
        {
            "court_slug": "bravo-court",
            "resource": "address",
            "method": "POST",
            "path": "/courts/00000000-0000-4000-a000-000000000002/v1/address",
            "status": "blocked",
            "http_status": None,
            "reason": (
                "Address verification requires review: FaCT/Ordnance Survey returned no "
                "address result: No address results returned from OS for postcode BB1 1BB"
            ),
        },
    ]

    report = _group_attention_by_request_type(actions)

    assert report[0]["label"] == "Addresses"
    assert report[0]["endpoint_templates"] == ["/courts/{court_id}/v1/address"]
    assert report[0]["distinct_outcome_count"] == 1
    assert report[0]["outcomes"][0]["action_count"] == 2
    assert report[0]["outcomes"][0]["court_count"] == 2
    assert report[0]["outcomes"][0]["classification"] == "address_review"


def test_get_execution_summary_rebuilds_an_old_cached_report(tmp_path):
    run_id = _archive(tmp_path)
    service = ApiExecutionService(tmp_path / "out", AppConfig(), FakeFactApiClient())
    service.store.save_summary(run_id, {"summary_version": "old", "run_id": run_id})

    summary = service.get_execution_summary(run_id)

    assert summary["summary_version"] == EXECUTION_SUMMARY_VERSION
    assert summary["planned_action_count"] == 1
    assert summary["replacement_approval_counts"]["not_checked"] == 1
    assert summary["court_hold_counts"]["held_by_approvals"] == 0
    assert summary["court_hold_counts"]["without_known_approval_hold"] == 1
    assert "review_progress_counts" in summary
    assert service.store.load_summary(run_id) == summary


def test_existing_legacy_plan_overlay_remains_active_after_a_newer_run(tmp_path):
    run_id = _archive(tmp_path)
    output_root = tmp_path / "out"
    archive = output_root / "final" / run_id
    report_path = archive / "api_readiness_report.json"
    report = json.loads(report_path.read_text())
    report["manifest_version"] = "1.8"
    report_path.write_text(json.dumps(report))
    (archive / "submissions_cleaned.json").write_text("[]")
    overlay = {
        "manifest_version": "1.9",
        "run_id": run_id,
        "derived_execution_overlay": True,
        "records": [
            {
                "court_slug": "overlay-court",
                "source_row_numbers": [9],
                "actions": [_action("overlay-action")],
            }
        ],
    }
    overlay_directory = output_root / "execution-review-state"
    overlay_directory.mkdir(parents=True, exist_ok=True)
    (overlay_directory / f"{run_id}.plan.json").write_text(json.dumps(overlay))
    (output_root / "latest_run.json").write_text(json.dumps({"run_id": "newer-run"}))

    readiness = ApiExecutionService(output_root).get_readiness_report(run_id)

    assert readiness["derived_execution_overlay"] is True
    assert readiness["records"][0]["court_slug"] == "overlay-court"


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


def _write_llm_review(
    tmp_path,
    run_id,
    review_ids,
    *,
    kind="field",
    api_body_patch=None,
    uprn=None,
    candidates=None,
):
    items = []
    for review_id in review_ids:
        item = {
            "review_id": review_id,
            "kind": kind,
            "source_row_number": 2,
            "court_slug": "example-court",
            "field": "addresses[1]" if kind == "address" else "facilities.food_and_drink",
            "outcome": "accepted",
            "actionable": True,
            "dependent_action_ids": ["example-court-1"],
            "model_result": {
                "value": "Normalised" if kind == "field" else None,
                "uprn": uprn,
                "confidence": "high",
                "needs_human_review": False,
                "reason": "Test result",
            },
            "llm_input": {"candidates": candidates or []},
            "api_body_patch": api_body_patch,
        }
        items.append(item)
    path = tmp_path / "out" / "final" / run_id / "llm_actions_review.json"
    path.write_text(
        json.dumps(
            {
                "review_version": "1.0",
                "item_count": len(items),
                "actionable_item_count": len(items),
                "items": items,
            }
        )
    )
