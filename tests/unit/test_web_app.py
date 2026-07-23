import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from zipfile import ZIP_STORED, ZipFile

import httpx
from werkzeug.datastructures import FileStorage

from fact_form_importer.config import AppConfig
from fact_form_importer.execution.fact_api import ApiResponse
from fact_form_importer.execution.models import ExecutionLedger
from fact_form_importer.execution.review_state import build_target_comparison
from fact_form_importer.execution.service import ApiExecutionService
from fact_form_importer.llm.review import field_review_id
from fact_form_importer.output.archive import publish_run_archive, stage_path
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.web.app import (
    LocalJobRunner,
    _action_review_tasks_by_source_row,
    _action_evidence,
    _action_execution_status,
    _change_has_hold,
    _comparison_summary,
    _load_readiness_report,
    _operational_submissions,
    _plain_record_action_reason,
    _raw_evidence_for_fields,
    _review_category,
    _record_issue_guidance,
    _review_overview,
    _safe_job_error,
    _status_from_visible_issues,
    _submission_has_review_category,
    _value_at_path,
    _workflow_payload,
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
    workflow = client.get(f"/runs/{run_id}/workflow")
    assert workflow.status_code == 200
    assert b"1. Review LLM and address results" in workflow.data
    assert b"2. Review changes to live FaCT data" in workflow.data
    assert b"3. Execute available API actions" in workflow.data
    assert b"This is not an approval queue" in workflow.data
    courts = client.get(f"/runs/{run_id}/courts")
    assert courts.status_code == 200
    assert b"View court actions" in courts.data
    court = client.get(f"/runs/{run_id}/courts/example-court")
    assert court.status_code == 200
    assert b"Refresh this court's live comparisons" in court.data
    assert client.get(f"/runs/{run_id}/records?status=processed").status_code == 200
    review = client.get(f"/runs/{run_id}/records?status=needs_human_review")
    assert b"review-court" in review.data
    detail = client.get(f"/runs/{run_id}/records/2")
    assert detail.status_code == 200
    assert b"Raw submitted values" in detail.data
    assert client.get(f"/runs/{run_id}/issues").status_code == 200
    assert b"Archived LLM review factors" in client.get(
        f"/runs/{run_id}/llm-review-factors"
    ).data
    assert b"Archived OS address review factors" in client.get(
        f"/runs/{run_id}/os-address-factors"
    ).data
    assert client.get(f"/runs/{run_id}/api-actions?readiness=ready").status_code == 200
    execution_page = client.get(f"/runs/{run_id}/execution-summary")
    assert execution_page.status_code == 200
    assert b"No API actions currently have blocked" in execution_page.data
    execution_json = client.get(f"/runs/{run_id}/execution-summary.json")
    assert execution_json.status_code == 200
    assert execution_json.headers["Content-Disposition"].startswith("attachment")
    assert b"Duplicate form decision workbook" in client.get(f"/runs/{run_id}").data
    landing_page = client.get("/").data
    assert b"Source submissions" in landing_page
    assert b"What the columns mean" in landing_page
    assert b"can grow when a reviewer supplies the correct FaCT court slug" in landing_page
    assert b"Authoritative submissions" in landing_page
    assert b"Superseded submissions" in landing_page


def test_landing_page_distinguishes_source_authoritative_and_superseded_rows(tmp_path):
    output_root, _ = _archive(tmp_path)
    manifest_path = next((output_root / "final").glob("*/run_manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    manifest["summary"].update(
        {
            "source_submission_count": 472,
            "authoritative_submission_count": 418,
            "superseded_submission_count": 54,
        }
    )
    manifest_path.write_text(json.dumps(manifest))

    page = create_app(output_root).test_client().get("/").data

    assert b">472</td><td>418</td><td>54</td>" in page
    assert b"Authoritative plus superseded submissions equals the source total" in page


def test_comparison_summary_categories_are_mutually_exclusive():
    summary = _comparison_summary(
        [
            {"comparison": {"is_no_change": True, "has_existing_data": True}},
            {"comparison": {"is_no_change": False, "has_existing_data": False}},
            {"comparison": {"is_no_change": False, "has_existing_data": True}},
            {
                "comparison": {
                    "is_no_change": True,
                    "has_existing_data": False,
                    "merge_conflicts": [{"business_type": "duplicate"}],
                }
            },
        ]
    )

    assert summary == {
        "total": 4,
        "checked": 4,
        "not_checked": 0,
        "no_change": 1,
        "empty_target": 1,
        "approval_required": 1,
        "approved": 0,
        "conflicts": 1,
    }
    assert sum(
        summary[key]
        for key in ("no_change", "empty_target", "approval_required", "approved", "conflicts")
    ) == summary["checked"]


def test_records_page_is_a_plain_language_todo_list_with_action_links(tmp_path):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    submissions_path = archive_path / "submissions_cleaned.json"
    submissions = json.loads(submissions_path.read_text())
    submissions[1]["issues"] = [
        {
            "field": "facilities.accessible_toilet_description",
            "code": "LLM_LOW_CONFIDENCE",
            "severity": "warning",
            "message": "Review the model result",
        },
        {
            "field": "opening_hours[1].opening_time",
            "code": "INVALID_TIME",
            "severity": "warning",
            "message": "Time is invalid",
        },
        {
            "field": "court_slug",
            "code": "COURT_SLUG_NORMALISED",
            "severity": "info",
            "message": "Court slug was cleaned",
        },
    ]
    submissions_path.write_text(json.dumps(submissions))
    readiness_path = archive_path / "api_readiness_report.json"
    readiness = json.loads(readiness_path.read_text())
    readiness["manifest_version"] = "99.0"
    review_action = dict(readiness["records"][0]["actions"][0])
    review_action.update(
        {
            "action_id": "review-court-1",
            "source_row_number": 3,
            "llm_review_ids": ["review-value"],
        }
    )
    readiness["records"].append(
        {
            "court_slug": "review-court",
            "source_row_numbers": [3],
            "actions": [review_action],
        }
    )
    readiness_path.write_text(json.dumps(readiness))
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps(
            {
                "review_version": "1.1",
                "items": [
                    {
                        "review_id": "review-value",
                        "kind": "field",
                        "source_row_number": 3,
                        "court_slug": "review-court",
                        "field": "facilities.accessible_toilet_description",
                        "model_result": {
                            "operation": "set",
                            "value": "Available on the ground floor.",
                            "confidence": "low",
                            "needs_human_review": True,
                        },
                        "outcome": "accepted",
                        "dependent_action_ids": ["review-court-1"],
                        "actionable": True,
                        "approvable": True,
                    }
                ],
            }
        )
    )

    client = create_app(output_root).test_client()
    page = client.get(f"/runs/{run_id}/records?status=needs_human_review")
    detail = client.get(f"/runs/{run_id}/records/3")

    assert page.status_code == 200
    assert b"complete source-review checklist" in page.data
    assert b"Decision needed in Step 1" in page.data
    assert b"Fix source and rerun" in page.data
    assert b"Information only" in page.data
    assert b"Review this row in Step 1" in page.data
    assert b"1 section action(s) planned" in page.data
    assert b"View court actions" in page.data
    assert b"/llm-actions?status=pending&amp;q=3" in page.data
    assert b"/courts/review-court" in page.data
    assert detail.status_code == 200
    assert b"What needs attention" in detail.data
    assert b"1 FaCT section action(s) are associated with this row" in detail.data


def test_live_review_total_includes_processed_pending_value_and_refreshes_on_approval(
    tmp_path,
):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    submissions_path = archive_path / "submissions_cleaned.json"
    submissions = json.loads(submissions_path.read_text())
    submissions[0]["issues"] = [
        {
            "field": "facilities.accessible_toilet_description",
            "code": "LLM_FIELD_NORMALISED",
            "severity": "info",
            "message": "LLM normalised a selected field",
        }
    ]
    submissions_path.write_text(json.dumps([submissions[0]]))
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps(
            {
                "review_version": "1.1",
                "items": [
                    {
                        "review_id": "processed-value",
                        "kind": "field",
                        "source_row_number": 2,
                        "court_slug": "example-court",
                        "field": "facilities.accessible_toilet_description",
                        "llm_input": {"cleaned_value": "Ground floor"},
                        "model_result": {
                            "operation": "set",
                            "value": "Available on the ground floor.",
                            "confidence": "medium",
                            "needs_human_review": False,
                            "reason": "Normalised wording",
                        },
                        "outcome": "accepted",
                        "dependent_action_ids": ["example-court-1"],
                        "actionable": True,
                        "approvable": True,
                    }
                ],
            }
        )
    )
    client = create_app(output_root).test_client()

    before = client.get(f"/runs/{run_id}/execution-summary.json").get_json()
    landing = client.get("/")
    outstanding = client.get(f"/runs/{run_id}/records?work=outstanding")
    ingestion_review = client.get(
        f"/runs/{run_id}/records?status=needs_human_review"
    )

    assert before["review_progress_counts"]["unique_courts_outstanding"] == 1
    assert before["review_progress_counts"]["pending_value_decisions"] == 1
    assert before["review_progress_counts"]["pending_value_decision_courts"] == 1
    assert b"Unique courts needing review" in landing.data
    assert b"work=outstanding" in landing.data
    assert b"example-court" in outstanding.data
    assert b"example-court" not in ingestion_review.data

    approved = client.post(
        f"/runs/{run_id}/llm-actions/processed-value/approve"
    )
    after = client.get(f"/runs/{run_id}/execution-summary.json").get_json()
    run_page = client.get(f"/runs/{run_id}")
    completed_queue = client.get(f"/runs/{run_id}/records?work=outstanding")

    assert approved.status_code == 302
    assert after["review_progress_counts"]["unique_courts_outstanding"] == 0
    assert after["review_progress_counts"]["pending_value_decisions"] == 0
    assert b"Form ingestion result" in run_page.data
    assert b"Current review progress" in run_page.data
    assert b"Needs source-data review" in run_page.data
    assert b"example-court" not in completed_queue.data


def test_review_queue_classification_and_hold_filters():
    assert _review_category("", "DUPLICATE_COURT_SLUG") == "court_identity_duplicates"
    assert _review_category("addresses[1].postcode") == "addresses"
    assert _submission_has_review_category(
        {
            "issues": [
                {
                    "field": "contacts[1].email",
                    "code": "INVALID_EMAIL",
                    "severity": "error",
                }
            ]
        },
        "contacts",
    )
    base = {
        "source_selection_required": True,
        "selected_source_row_number": None,
        "comparison": None,
        "action": {"readiness": "pending", "reason": "invalid body"},
        "execution_status": "blocked",
    }
    assert _change_has_hold(base, "source_selection")
    assert _change_has_hold(base, "target_not_checked")
    assert _change_has_hold(base, "invalid_request")
    assert _change_has_hold(base, "execution_attention")
    address = {
        **base,
        "action": {
            "readiness": "pending",
            "reason": "Address verification requires review",
        },
    }
    assert _change_has_hold(address, "os_resolution")
    assert not _change_has_hold(address, "invalid_request")
    replacement = {
        **base,
        "comparison": {"has_existing_data": True, "is_no_change": False},
        "target_approved": False,
    }
    assert _change_has_hold(replacement, "target_replacement")
    assert _change_has_hold(replacement, "unknown-filter")


def test_record_action_tasks_and_issue_guidance_cover_each_human_remedy():
    changes = [
        {"source_row_number": None, "source_row_numbers": [2, 3]},
        {
            "source_row_number": 2,
            "comparison": {"merge_conflicts": ["duplicate contact type"]},
            "action": {"readiness": "ready"},
        },
        {
            "source_row_number": 2,
            "comparison": {"merge_conflicts": ["another conflict"]},
            "action": {"readiness": "ready"},
        },
        {
            "source_row_numbers": [3],
            "comparison": {"has_existing_data": True, "is_no_change": False},
            "target_approved": False,
            "action": {"readiness": "ready"},
        },
        {
            "source_row_number": 4,
            "comparison": None,
            "action": {
                "readiness": "pending",
                "reason": "courtId and addressLine1 are required",
            },
        },
        {
            "source_row_number": 5,
            "comparison": {"has_existing_data": False, "is_no_change": True},
            "action": {"readiness": "ready"},
        },
    ]

    tasks = _action_review_tasks_by_source_row(changes)

    assert tasks[2] == [
        {
            "state": "changes",
            "label": "Resolve an ambiguous FaCT match in Step 2",
            "remedy": "Review the live and proposed typed entries before this section can run.",
        }
    ]
    assert tasks[3][0]["label"] == "Approve a live FaCT change in Step 2"
    assert tasks[4][0]["state"] == "court"
    assert "court identifier" in tasks[4][0]["remedy"]
    assert "address line 1" in tasks[4][0]["remedy"]
    assert 5 not in tasks
    assert _plain_record_action_reason(None) is None

    court = _record_issue_guidance(
        {"code": "COURT_SLUG_NOT_FOUND", "field": "court_slug"}, []
    )
    address = _record_issue_guidance(
        {"code": "ADDRESS_OS_REVIEW_REQUIRED", "field": "addresses[1]"}, []
    )
    pending = _record_issue_guidance(
        {"code": "LLM_LOW_CONFIDENCE", "field": "contacts[1].explanation"},
        [{"approval_status": "pending"}],
    )
    complete = _record_issue_guidance(
        {"code": "LLM_FIELD_NORMALISED", "field": "contacts[1].explanation"},
        [{"approval_status": "approved"}],
    )

    assert court["state"] == "source" and "cannot create or guess" in court["remedy"]
    assert address["state"] == "source" and "No approvable address" in address["remedy"]
    assert pending["state"] == "review"
    assert complete["state"] == "complete"


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
    assert b"Model and address value decisions" in page.data
    assert b"Free water dispensers" in page.data
    assert b"Approve" in page.data

    approved = client.post(f"/runs/{run_id}/llm-actions/{review_id}/approve")

    assert approved.status_code == 302
    assert execution_client.writes == []
    refreshed = client.get(f"/runs/{run_id}/llm-actions")
    assert b"Approved" in refreshed.data
    assert service.get_execution_summary(run_id)["llm_approval_counts"]["approved"] == 1


def test_llm_approval_advances_to_next_item_and_preserves_confidence_filter(tmp_path):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    items = []
    for row, review_id, confidence in (
        (3, "medium-review", "medium"),
        (2, "first-high", "high"),
        (4, "second-high", "high"),
    ):
        items.append(
            {
                "review_id": review_id,
                "kind": "field",
                "source_row_number": row,
                "court_slug": "example-court",
                "field": "facilities.food_and_drink",
                "llm_input": {"raw_value": "water", "cleaned_value": "water"},
                "model_result": {
                    "value": ["Free water dispensers"],
                    "confidence": confidence,
                    "needs_human_review": confidence != "high",
                    "reason": "Test result",
                },
                "outcome": "accepted",
                "dependent_action_ids": ["example-court-1"],
                "actionable": True,
            }
        )
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps({"review_version": "1.1", "items": items})
    )
    client = create_app(output_root).test_client()

    page = client.get(f"/runs/{run_id}/llm-actions?status=pending&confidence=high")
    approved = client.post(
        f"/runs/{run_id}/llm-actions/first-high/approve",
        data={
            "review_status": "pending",
            "review_confidence": "high",
            "review_query": "",
            "review_queue": "",
            "field_page": "1",
            "address_page": "1",
        },
    )

    assert page.status_code == 200
    assert page.data.index(b"first-high") < page.data.index(b"second-high")
    assert b"medium-review" not in page.data
    assert b"confirm(" not in page.data
    assert approved.status_code == 302
    assert "confidence=high" in approved.headers["Location"]
    assert "status=pending" in approved.headers["Location"]
    assert approved.headers["Location"].endswith("#review-second-high")


def test_llm_deny_then_bulk_approve_excludes_denied_and_never_writes(tmp_path):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    items = []
    for row, review_id in ((2, "deny-me"), (3, "approve-one"), (4, "approve-two")):
        items.append(
            {
                "review_id": review_id,
                "kind": "field",
                "source_row_number": row,
                "court_slug": "example-court",
                "field": "facilities.food_and_drink",
                "llm_input": {"raw_value": "water", "cleaned_value": "water"},
                "model_result": {
                    "operation": "set",
                    "value": ["Free water dispensers"],
                    "confidence": "medium",
                    "needs_human_review": True,
                    "reason": "Test result",
                },
                "outcome": "accepted",
                "dependent_action_ids": ["example-court-1"],
                "actionable": True,
            }
        )
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps({"review_version": "1.1", "items": items})
    )
    execution_client = _FakeExecutionClient()
    service = ApiExecutionService(output_root, AppConfig(), execution_client)
    client = create_app(output_root, execution_service=service).test_client()

    pending = client.get(f"/runs/{run_id}/llm-actions?status=pending")
    denied = client.post(
        f"/runs/{run_id}/llm-actions/deny-me/deny",
        data={
            "review_status": "pending",
            "field_page": "1",
            "address_page": "1",
            "rationale": "This does not match the submitted evidence",
        },
    )
    denied_page = client.get(f"/runs/{run_id}/llm-actions?status=denied")
    bulk = client.post(f"/runs/{run_id}/llm-actions/approve-all")
    bulk_page = client.get(bulk.headers["Location"])

    assert b"Deny and continue" in pending.data
    assert b"Approve all eligible remaining results" in pending.data
    assert b"Fast-forward all Step 1 decisions for testing" in pending.data
    assert denied.headers["Location"].endswith("#review-approve-one")
    assert b"Reconsider" in denied_page.data
    assert "bulk_approved=2" in bulk.headers["Location"]
    assert b"2 remaining LLM review approval(s) recorded" in bulk_page.data
    review = service.get_llm_actions_review(run_id)
    assert review["approval_counts"]["review_pending"] == 0
    assert review["approval_counts"]["review_denied"] == 1
    assert service.get_execution_summary(run_id)["llm_approval_counts"]["denied"] == 1
    assert execution_client.writes == []

    reconsidered = client.post(
        f"/runs/{run_id}/llm-actions/deny-me/reconsider",
        data={"review_status": "denied", "field_page": "1", "address_page": "1"},
    )

    assert "status=pending" in reconsidered.headers["Location"]
    assert reconsidered.headers["Location"].endswith("#review-deny-me")
    assert execution_client.writes == []


def test_testing_fast_forward_routes_redirect_to_the_next_workflow_step(tmp_path):
    output_root, run_id = _archive(tmp_path)

    class LlmFastForwardService:
        def apply_test_llm_decisions(self, requested_run_id):
            assert requested_run_id == run_id
            return {
                "approved": 7,
                "candidate_selections": 3,
                "edited_fields": 1,
                "invalidated_actions": 2,
                "skipped": 2,
            }

    class InactiveRunner:
        def active(self):
            return None

        def start(self, requested_run_id, scope):
            assert requested_run_id == run_id
            assert scope == "comparison"
            return SimpleNamespace(job_id="comparison-job")

    llm_app = create_app(
        output_root, execution_service=LlmFastForwardService()
    )
    llm_app.config["EXECUTION_JOB_RUNNER"] = InactiveRunner()
    llm_client = llm_app.test_client()
    llm_result = llm_client.post(
        f"/runs/{run_id}/llm-actions/test-fast-forward"
    )

    assert llm_result.status_code == 302
    assert "/api-changes?" in llm_result.headers["Location"]
    assert "test_llm_approved=7" in llm_result.headers["Location"]
    assert "test_candidates=3" in llm_result.headers["Location"]
    assert "job_id=comparison-job" in llm_result.headers["Location"]

    class ChangesFastForwardService:
        def apply_test_api_change_decisions(self, requested_run_id):
            assert requested_run_id == run_id
            return {
                "approved_changes": 11,
                "omitted_items": 8,
                "resolved_sections": 2,
                "skipped_conflicts": 4,
            }

    changes_app = create_app(
        output_root, execution_service=ChangesFastForwardService()
    )
    changes_app.config["EXECUTION_JOB_RUNNER"] = InactiveRunner()
    changes_client = changes_app.test_client()
    changes_result = changes_client.post(
        f"/runs/{run_id}/api-changes/test-fast-forward"
    )

    assert changes_result.status_code == 302
    assert "test_changes=11" in changes_result.headers["Location"]
    assert "test_omitted=8" in changes_result.headers["Location"]
    assert "test_conflicts_skipped=4" in changes_result.headers["Location"]

    class RaisingService:
        def apply_test_llm_decisions(self, requested_run_id):
            raise ValueError("Unsafe test value")

        def apply_test_api_change_decisions(self, requested_run_id):
            raise ValueError("Unsafe test change")

    raising_app = create_app(output_root, execution_service=RaisingService())
    raising_app.config["EXECUTION_JOB_RUNNER"] = InactiveRunner()
    raising_client = raising_app.test_client()
    assert (
        raising_client.post(
            f"/runs/{run_id}/llm-actions/test-fast-forward"
        ).status_code
        == 400
    )
    assert (
        raising_client.post(
            f"/runs/{run_id}/api-changes/test-fast-forward"
        ).status_code
        == 400
    )

    class ActiveRunner:
        def active(self):
            return SimpleNamespace(job_id="active")

    active_app = create_app(output_root, execution_service=RaisingService())
    active_app.config["EXECUTION_JOB_RUNNER"] = ActiveRunner()
    active_client = active_app.test_client()
    assert (
        active_client.post(
            f"/runs/{run_id}/llm-actions/test-fast-forward"
        ).status_code
        == 400
    )
    assert (
        active_client.post(
            f"/runs/{run_id}/api-changes/test-fast-forward"
        ).status_code
        == 400
    )


def test_missing_court_target_can_be_validated_without_writing_and_collision_is_rejected(
    tmp_path,
):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    submissions_path = archive_path / "submissions_cleaned.json"
    submissions = json.loads(submissions_path.read_text())
    submissions[1] = {
        "source": {"source_row_number": 3},
        "court_slug": "missing-court",
        "court_slug_raw": "Missing Court",
        "status": "needs_human_review",
        "facilities": {"parking_available": True},
        "raw": {"court_slug": "Missing Court"},
        "issues": [
            {
                "field": "court_slug",
                "code": "COURT_SLUG_NOT_FOUND",
                "severity": "warning",
                "message": "Court slug does not exist in FaCT Data API",
                "raw_value": "missing-court",
                "cleaned_value": {
                    "suggested_slug": "suggested-court",
                    "suggested_court_name": "Suggested Court",
                },
            }
        ],
    }
    submissions_path.write_text(json.dumps(submissions))
    readiness_path = archive_path / "api_readiness_report.json"
    readiness = json.loads(readiness_path.read_text())
    readiness["manifest_version"] = "1.9"
    readiness_path.write_text(json.dumps(readiness))
    review_id = field_review_id(3, "facilities.parking_available")
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps(
            {
                "review_version": "1.1",
                "items": [
                    {
                        "review_id": review_id,
                        "kind": "field",
                        "source_row_number": 3,
                        "court_slug": "missing-court",
                        "field": "facilities.parking_available",
                        "llm_input": {"cleaned_value": True},
                        "model_result": {
                            "operation": "set",
                            "value": True,
                            "confidence": "medium",
                            "needs_human_review": True,
                            "reason": "Test result",
                        },
                        "outcome": "accepted",
                        "dependent_action_ids": [],
                        "actionable": False,
                        "approvable": True,
                    }
                ],
            }
        )
    )
    execution_client = _FakeExecutionClient()
    service = ApiExecutionService(output_root, AppConfig(), execution_client)
    client = create_app(output_root, execution_service=service).test_client()

    existing_comparison = build_target_comparison(
        "example-court",
        {
            "action_id": "example-court-1",
            "resource": "building_facilities",
            "source_row_number": 2,
            "body": {"parking": True},
        },
        {"parking": False},
    )
    service.review_store.save_comparison(run_id, existing_comparison)
    service.review_store.approve_target(run_id, existing_comparison.change_id)

    page = client.get(f"/runs/{run_id}/llm-actions?status=pending&q=3")
    collision = client.post(
        f"/runs/{run_id}/records/3/court-target",
        data={"target_slug": "example-court", "review_id": review_id},
    )
    selected = client.post(
        f"/runs/{run_id}/records/3/court-target",
        data={"target_slug": "validated-court", "review_id": review_id},
    )

    assert b"Validate and use this court" in page.data
    assert b"suggested-court" in page.data
    assert "court_target_error=" in collision.headers["Location"]
    collision_error = parse_qs(urlparse(collision.headers["Location"]).query)[
        "court_target_error"
    ][0]
    assert "already targeted by source row(s) 2" in collision_error
    assert "court_target_saved=validated-court" in selected.headers["Location"]
    override = service.get_execution_review(run_id).court_target_overrides["3"]
    assert override.target_slug == "validated-court"
    preserved_review = service.get_execution_review(run_id)
    assert existing_comparison.change_id in preserved_review.comparisons
    assert existing_comparison.change_id in preserved_review.target_approvals
    record = next(
        value
        for value in service.get_readiness_report(run_id)["records"]
        if value["court_slug"] == "validated-court"
    )
    assert record["actions"]
    review = service.get_llm_actions_review(run_id)["items"][0]
    assert review["actionable"] is True
    assert review["dependent_actions"]
    resolved_record_page = client.get(f"/runs/{run_id}/records/3")
    assert b"Resolved with a validated FaCT court target" in resolved_record_page.data
    assert b"validated-court" in resolved_record_page.data
    assert execution_client.writes == []


def test_duplicate_item_resolution_unactionable_closure_and_business_downloads(tmp_path):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    readiness_path = archive_path / "api_readiness_report.json"
    readiness = json.loads(readiness_path.read_text())
    readiness["manifest_version"] = "2.0"
    action = readiness["records"][0]["actions"][0]
    action.update(
        {
            "resource": "contact_detail",
            "proposed_items": [
                    {
                        "courtContactDescriptionId": "old-type",
                        "email": "first@example.test",
                        "phoneNumber": "020 1111 1111",
                    },
                {
                    "courtContactDescriptionId": "old-type",
                    "email": "duplicate@example.test",
                },
            ],
            "proposed_item_ids": ["source-item-1", "source-item-2"],
            "proposed_item_source_fields": [["contacts[1]"], ["contacts[2]"]],
        }
    )
    readiness_path.write_text(json.dumps(readiness))
    (archive_path / "fact_vocabularies.json").write_text(
        json.dumps(
            {
                "vocabularies": {
                    "contact_description_types": [
                        {"name": "Enquiries", "code": "ENQUIRIES", "api_id": "new-type"}
                    ]
                }
            }
        )
    )
    submissions_path = archive_path / "submissions_cleaned.json"
    submissions = json.loads(submissions_path.read_text())
    submissions[1]["issues"] = [
        {
            "field": "court_slug",
            "code": "COURT_SLUG_NOT_FOUND",
            "message": "Court was not found",
        }
    ]
    submissions_path.write_text(json.dumps(submissions))
    execution_client = _FakeExecutionClient()
    service = ApiExecutionService(output_root, AppConfig(), execution_client)
    client = create_app(output_root, execution_service=service).test_client()
    comparison = build_target_comparison("example-court", action, [])
    service.review_store.save_comparison(run_id, comparison)

    review_page = client.get(f"/runs/{run_id}/api-changes")
    assert b"Testing aid: omit every submitted entry in this section" in review_page.data
    assert b"Omit all submitted entries in this section" in review_page.data

    remapped = client.post(
        f"/runs/{run_id}/api-changes/example-court-1/items/source-item-1/resolve",
        data={
            "decision": "remap",
            "replacement_type_id": "new-type",
            "rationale": "The reviewer confirmed the enquiries type",
            "change_id": "change",
        },
    )
    closed = client.post(
        f"/runs/{run_id}/records/3/close-unactionable",
        data={"rationale": "No defensible FaCT court match exists"},
    )

    assert remapped.status_code == 302
    resolution = service.get_execution_review(run_id).collection_item_resolutions[
        "source-item-1"
    ]
    assert resolution.replacement_type_id == "new-type"
    omitted = client.post(
        f"/runs/{run_id}/api-changes/example-court-1/items/omit-all",
        data={
            "rationale": "Testing confirmed that all duplicate submitted contacts should be ignored",
            "change_id": comparison.change_id,
        },
    )
    assert omitted.status_code == 302
    assert "omitted=2" in omitted.headers["Location"]
    resolutions = service.get_execution_review(run_id).collection_item_resolutions
    assert {
        item_id: value.decision for item_id, value in resolutions.items()
    } == {"source-item-1": "omit", "source-item-2": "omit"}
    assert execution_client.writes == []
    assert closed.status_code == 302
    assert service.get_execution_review(run_id).court_dispositions["3"].rationale == (
        "No defensible FaCT court match exists"
    )
    assert client.get(f"/runs/{run_id}/business-report").status_code == 200
    assert client.get(f"/runs/{run_id}/business-report.md").status_code == 200
    assert client.get(f"/runs/{run_id}/business-report.csv").status_code == 200
    assert client.get(f"/runs/{run_id}/business-report.json").status_code == 200


def test_review_mutation_routes_return_plain_400_errors_and_record_redirects(tmp_path):
    output_root, run_id = _archive(tmp_path)
    client = create_app(output_root).test_client()

    assert client.post(
        f"/runs/{run_id}/records/3/close-unactionable", data={"rationale": ""}
    ).status_code == 400
    assert client.post(
        f"/runs/{run_id}/api-changes/missing/items/missing/resolve",
        data={"decision": "invalid", "rationale": "Test"},
    ).status_code == 400
    assert client.post(
        f"/runs/{run_id}/api-changes/missing/items/omit-all",
        data={"rationale": ""},
    ).status_code == 400
    assert client.post(
        f"/runs/{run_id}/api-changes/missing/refresh",
        data={"court_slug": "missing"},
    ).status_code == 400
    assert client.post(
        f"/runs/{run_id}/api-changes/missing/approve"
    ).status_code == 400
    invalid_target = client.post(
        f"/runs/{run_id}/records/3/court-target", data={"target_slug": "INVALID SLUG"}
    )
    assert invalid_target.status_code == 302
    assert "/records/3?" in invalid_target.headers["Location"]


def test_bulk_change_approval_surfaces_validation_error(tmp_path, monkeypatch):
    output_root, run_id = _archive(tmp_path)
    service = ApiExecutionService(output_root, AppConfig(), _FakeExecutionClient())

    def fail(_run_id):
        raise ValueError("comparison changed")

    monkeypatch.setattr(service, "approve_all_target_changes", fail)
    client = create_app(output_root, execution_service=service).test_client()

    response = client.post(f"/runs/{run_id}/api-changes/approve-all")

    assert response.status_code == 400
    assert b"comparison changed" in response.data


def test_ui_import_starts_retryable_comparison_and_keeps_archive_on_scan_failure(tmp_path):
    started = []

    def processor(**kwargs):
        return SimpleNamespace(
            run_id="new-run",
            output=SimpleNamespace(summary={"vocabulary_source": "fact_data_api"}),
        )

    def comparison_starter(run_id):
        started.append(run_id)
        raise ValueError("temporary comparison failure")

    runner = LocalJobRunner(tmp_path, processor, comparison_starter)
    job = runner.start(
        FileStorage(stream=io.BytesIO(b"a,b\n"), filename="forms.csv"),
        use_llm=False,
        llm_enabled=False,
    )
    runner.executor.shutdown(wait=True)

    assert runner.get(job.job_id).state == "completed"
    assert started == ["new-run"]


def test_llm_review_explains_missing_court_in_plain_english(tmp_path):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    submissions_path = archive_path / "submissions_cleaned.json"
    submissions = json.loads(submissions_path.read_text())
    submissions[0]["issues"] = [
        {
            "field": "court_slug",
            "code": "COURT_SLUG_NOT_FOUND",
            "severity": "warning",
            "message": "Court slug does not exist in FaCT Data API",
        }
    ]
    submissions_path.write_text(json.dumps(submissions))
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps(
            {
                "review_version": "1.1",
                "items": [
                    {
                        "review_id": "blocked-review",
                        "kind": "field",
                        "source_row_number": 2,
                        "court_slug": "missing-court",
                        "field": "facilities.accessible_toilet_description",
                        "llm_input": {"cleaned_value": "Ground floor"},
                        "model_result": {
                            "operation": "set",
                            "value": "Available on the ground floor.",
                            "confidence": "high",
                            "needs_human_review": False,
                            "reason": "Normalised wording",
                        },
                        "outcome": "accepted",
                        "dependent_action_ids": [],
                        "actionable": False,
                        "approvable": True,
                    }
                ],
            }
        )
    )

    page = create_app(output_root).test_client().get(f"/runs/{run_id}/llm-actions")

    assert page.status_code == 200
    assert b"This court could not be found in the FaCT database." in page.data
    assert b"Technical details" in page.data
    assert b"No API action was planned because" not in page.data


def test_address_review_fields_are_editable_and_posted_value_is_approved(tmp_path):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    readiness_path = archive_path / "api_readiness_report.json"
    readiness = json.loads(readiness_path.read_text())
    action = readiness["records"][0]["actions"][0]
    action["resource"] = "address"
    action["llm_review_ids"] = ["address-review"]
    readiness_path.write_text(json.dumps(readiness))
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps(
            {
                "review_version": "1.1",
                "items": [
                    {
                        "review_id": "address-review",
                        "kind": "address",
                        "source_row_number": 2,
                        "court_slug": "example-court",
                        "field": "addresses[1]",
                        "address_index": 1,
                        "submitted_address": {"line_1": "Submitted Court"},
                        "llm_input": {
                            "candidates": [
                                {"uprn": "uprn-1"},
                                {"uprn": "uprn-2"},
                            ]
                        },
                        "os_candidates": [{"uprn": "uprn-1"}],
                        "model_result": {
                            "uprn": "uprn-1",
                            "confidence": "high",
                            "needs_human_review": False,
                            "reason": "Selected candidate",
                        },
                        "api_body_patch": {
                            "addressLine1": "OS Court",
                            "addressLine2": None,
                            "townCity": "London",
                            "county": None,
                            "postcode": "SW1A 1AA",
                        },
                        "proposed_address": {
                            "line_1": "OS Court",
                            "town_or_city": "London",
                            "postcode": "SW1A 1AA",
                        },
                        "outcome": "accepted",
                        "dependent_action_ids": ["example-court-1"],
                        "actionable": True,
                        "approvable": True,
                    }
                ],
            }
        )
    )
    service = ApiExecutionService(output_root, AppConfig(), _FakeExecutionClient())
    client = create_app(output_root, execution_service=service).test_client()

    page = client.get(f"/runs/{run_id}/llm-actions")
    approved = client.post(
        f"/runs/{run_id}/llm-actions/address-review/approve",
        data={
            "addressLine1": "Reviewer Court",
            "addressLine2": "PO Box 12",
            "townCity": "London",
            "county": "Greater London",
            "postcode": "SW1A 1AA",
            "field_page": "1",
            "address_page": "1",
        },
    )

    assert page.status_code == 200
    assert b'name="addressLine1"' in page.data
    assert b"Address type, areas of law, court types and the selected UPRN remain unchanged" in page.data
    assert approved.status_code == 302
    approval = service.approval_store.load(run_id).approvals["address-review"]
    assert approval.approved_address_patch["addressLine1"] == "Reviewer Court"
    assert approval.approved_address_patch["addressLine2"] == "PO Box 12"


def test_unresolved_address_renders_manual_candidate_selector_and_requires_reason(
    tmp_path,
):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    readiness_path = archive_path / "api_readiness_report.json"
    readiness = json.loads(readiness_path.read_text())
    action = readiness["records"][0]["actions"][0]
    action.update(
        {
            "resource": "address",
            "readiness": "pending",
            "reason": "Address verification requires review: Multiple candidates",
            "source_fields": ["addresses[1]"],
            "source_row_number": 2,
            "address_verification": {
                "status": "review_required",
                "message": "Multiple candidates",
            },
        }
    )
    readiness_path.write_text(json.dumps(readiness))
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps(
            {
                "review_version": "1.1",
                "items": [
                    {
                        "review_id": "manual-address",
                        "kind": "address",
                        "source_row_number": 2,
                        "court_slug": "example-court",
                        "field": "addresses[1]",
                        "address_index": 1,
                        "submitted_address": {
                            "index": 1,
                            "address_type": "Visit",
                            "line_1": "Submitted Court",
                            "town_or_city": "London",
                            "postcode": "SW1A 1AA",
                        },
                        "llm_input": {"candidates": [{"uprn": "uprn-1"}]},
                        "os_candidates": [
                            {
                                "uprn": "uprn-1",
                                "address": None,
                                "organisation_name": "Selected Court",
                                "building_number": "1",
                                "building_name": None,
                                "thoroughfare_name": "Main Street",
                                "post_town": "London",
                                "postcode": "SW1A 1AA",
                            }
                        ],
                        "model_result": {
                            "uprn": None,
                            "confidence": "low",
                            "needs_human_review": True,
                            "reason": "No unique selection",
                        },
                        "outcome": "no_selection",
                        "dependent_action_ids": ["example-court-1"],
                        "actionable": False,
                        "approvable": False,
                    }
                ],
            }
        )
    )
    service = ApiExecutionService(output_root, AppConfig(), _FakeExecutionClient())
    client = create_app(output_root, execution_service=service).test_client()

    page = client.get(f"/runs/{run_id}/llm-actions?status=pending")
    missing_reason = client.post(
        f"/runs/{run_id}/llm-actions/manual-address/approve",
        data={"selected_uprn": "uprn-1"},
    )
    approved = client.post(
        f"/runs/{run_id}/llm-actions/manual-address/approve",
        data={
            "selected_uprn": "uprn-1",
            "selection_rationale": "The organisation and street identify the court",
            "addressLine1": "Selected Court",
            "addressLine2": "1 Main Street",
            "townCity": "London",
            "postcode": "SW1A 1AA",
        },
    )

    assert page.status_code == 200
    assert b"Select the OS address candidate to use" in page.data
    assert b'name="selected_uprn"' in page.data
    assert b"Reason for selecting this candidate" in page.data
    assert missing_reason.status_code == 400
    assert approved.status_code == 302
    decision = service.approval_store.load(run_id).approvals["manual-address"]
    assert decision.selected_uprn == "uprn-1"
    assert decision.rationale.startswith("The organisation")


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
    assert b"high-supplied-os-candidate-v2" in page.data
    assert b"Save address and continue" not in page.data
    assert b"Edit approved address" in page.data
    assert b"Save edited address" in page.data
    assert b"Actions without a review hold" in summary.data
    assert service.get_execution_summary(run_id)["llm_approval_counts"]["auto_approved"] == 1
    assert execution_client.writes == []


def test_review_overview_and_api_change_routes_cover_value_source_and_target_gates(
    tmp_path,
):
    output_root, run_id = _archive(tmp_path)
    archive_path = output_root / "final" / run_id
    readiness_path = archive_path / "api_readiness_report.json"
    readiness = json.loads(readiness_path.read_text())
    record = readiness["records"][0]
    record["source_row_numbers"] = [2, 3]
    action = record["actions"][0]
    action["source_row_number"] = 2
    action["source_selection_required"] = True
    action["llm_review_ids"] = ["field-review"]
    readiness_path.write_text(json.dumps(readiness))
    (archive_path / "llm_actions_review.json").write_text(
        json.dumps(
            {
                "review_version": "1.1",
                "items": [
                    {
                        "review_id": "field-review",
                        "kind": "field",
                        "source_row_number": 2,
                        "court_slug": "example-court",
                        "field": "facilities.parking_available",
                        "llm_input": {"raw_value": "Yes", "cleaned_value": True},
                        "model_result": {
                            "operation": "set",
                            "value": False,
                            "confidence": "medium",
                            "needs_human_review": False,
                            "reason": "Safe test result",
                        },
                        "outcome": "accepted",
                        "dependent_action_ids": ["example-court-1"],
                        "actionable": True,
                        "approvable": True,
                    }
                ],
            }
        )
    )

    execution_client = _ExistingTargetExecutionClient()
    service = ApiExecutionService(output_root, AppConfig(), execution_client)
    app = create_app(output_root, config=AppConfig(), execution_service=service)
    client = app.test_client()

    overview = client.get(f"/runs/{run_id}/review")
    assert overview.status_code == 200
    assert b"Source Selection" in overview.data
    assert b"Llm Approval" in overview.data
    assert (
        client.get(
            f"/runs/{run_id}/records?status=needs_human_review"
            "&category=facilities_accessibility"
        ).status_code
        == 200
    )
    assert (
        client.get(f"/runs/{run_id}/llm-actions?status=pending&queue=llm").status_code
        == 200
    )

    selected = client.post(
        f"/runs/{run_id}/courts/example-court/select-source",
        data={"source_row_number": "2"},
    )
    approved_value = client.post(f"/runs/{run_id}/llm-actions/field-review/approve")
    refreshed = client.post(
        f"/runs/{run_id}/api-changes/example-court-1/refresh",
        data={"court_slug": "example-court"},
    )
    change = service.get_api_changes_review(run_id)["changes"][0]
    approved_target = client.post(
        f"/runs/{run_id}/api-changes/{change['change_id']}/approve"
    )
    changes_page = client.get(f"/runs/{run_id}/api-changes?view=all")

    assert {selected.status_code, approved_value.status_code, refreshed.status_code} == {302}
    assert approved_target.status_code == 302
    assert "view=pending" in approved_target.headers["Location"]
    assert "completed=1" in approved_target.headers["Location"]
    assert changes_page.status_code == 200
    assert b"This is not a 1-item approval queue" in changes_page.data
    assert b"planned section actions compared with live FaCT" in changes_page.data
    assert b"effective difference" in changes_page.data
    assert execution_client.writes == []
    assert client.get(
        f"/runs/{run_id}/api-changes?hold=target_replacement"
    ).status_code == 200
    assert client.post(
        f"/runs/{run_id}/llm-actions/missing-review/approve"
    ).status_code == 400
    assert client.post(
        f"/runs/{run_id}/api-changes/missing-action/refresh",
        data={"court_slug": "example-court"},
    ).status_code == 400
    assert client.post(
        f"/runs/{run_id}/api-changes/missing-change/approve"
    ).status_code == 400
    assert client.post(
        f"/runs/{run_id}/courts/example-court/select-source",
        data={"source_row_number": "not-a-number"},
    ).status_code == 400
    assert client.get("/execution-jobs/missing/status.json").status_code == 404

    scan = client.post(f"/runs/{run_id}/api-changes/refresh")
    assert scan.status_code == 302
    scan_job = _wait_for_execution_job(app)
    assert scan_job.state == "completed"
    assert client.get(f"/execution-jobs/{scan_job.job_id}/status.json").status_code == 200


def test_api_changes_review_has_previous_and_next_navigation(tmp_path):
    output_root, run_id = _archive(tmp_path)
    changes = [
        {
            "change_id": f"change-{index}",
            "court_slug": f"court-{index}",
            "source_row_number": index + 2,
            "source_selection_required": False,
            "selected_source_row_number": None,
            "action": {
                "action_id": f"action-{index}",
                "resource": "building_facilities",
                "readiness": "ready",
            },
            "comparison": None,
            "target_approved": False,
            "pending_value_holds": [],
            "execution_status": "planned",
        }
        for index in range(51)
    ]

    class ChangesService:
        def get_api_changes_review(self, requested_run_id):
            return {"changes": changes}

    client = create_app(output_root, execution_service=ChangesService()).test_client()

    first = client.get(f"/runs/{run_id}/api-changes?hold=target_not_checked")
    second = client.get(f"/runs/{run_id}/api-changes?hold=target_not_checked&page=2")

    assert first.status_code == 200
    assert b"Page 1 of 2" in first.data
    assert b"Next page" in first.data
    assert b"Previous page" not in first.data
    assert b"page=2" in first.data
    assert b"hold=target_not_checked" in first.data
    assert second.status_code == 200
    assert b"Page 2 of 2" in second.data
    assert b"Previous page" in second.data
    assert b"Next page" not in second.data


def test_api_changes_defaults_to_pending_advances_approval_and_hides_stale_job(
    tmp_path,
):
    output_root, run_id = _archive(tmp_path)

    def change(index, *, existing=True):
        return {
            "change_id": f"change-{index}",
            "court_slug": f"court-{index}",
            "source_row_number": index + 2,
            "source_selection_required": False,
            "selected_source_row_number": None,
            "action": {
                "action_id": f"action-{index}",
                "resource": "building_facilities",
                "readiness": "ready",
            },
            "comparison": {
                "current": {"parking": False} if existing else {},
                "submitted": {"parking": True},
                "proposed": {"parking": True},
                "operations": [],
                "differences": [],
                "has_existing_data": existing,
                "is_no_change": False,
                "merge_conflicts": [],
            },
            "target_approved": False,
            "pending_value_holds": [],
            "execution_status": "planned",
        }

    changes = [change(1), change(2), change(3, existing=False)]

    class ChangesService:
        def get_api_changes_review(self, requested_run_id):
            assert requested_run_id == run_id
            return {"changes": changes}

        def approve_target_change(self, requested_run_id, change_id):
            assert requested_run_id == run_id
            target = next(item for item in changes if item["change_id"] == change_id)
            target["target_approved"] = True

        def approve_all_target_changes(self, requested_run_id):
            assert requested_run_id == run_id
            eligible = [
                item
                for item in changes
                if item["comparison"]["has_existing_data"]
                and not item["comparison"]["merge_conflicts"]
                and not item["target_approved"]
            ]
            for item in eligible:
                item["target_approved"] = True
            return None, len(eligible)

    jobs = output_root / ".execution-jobs"
    jobs.mkdir(parents=True)
    (jobs / "stale.json").write_text(
        json.dumps(
            {
                "job_id": "stale",
                "run_id": run_id,
                "scope": "comparison",
                "state": "interrupted",
                "created_at": "2026-07-15T10:00:00Z",
                "completed_at": "2026-07-15T10:01:00Z",
                "error": "Server restarted before the execution job completed",
            }
        )
    )
    client = create_app(output_root, execution_service=ChangesService()).test_client()

    pending = client.get(f"/runs/{run_id}/api-changes")
    all_sections = client.get(f"/runs/{run_id}/api-changes?view=all")
    advanced = client.post(
        f"/runs/{run_id}/api-changes/change-1/approve",
        data={"view": "pending"},
    )

    assert pending.status_code == 200
    assert b"Human decisions pending (2)" in pending.data
    assert b"court-1" in pending.data
    assert b"court-3" not in pending.data
    assert b"Approve and continue" in pending.data
    assert b"Approve all 2 live-data changes" in pending.data
    assert b"Fast-forward all Step 2 decisions for testing" in pending.data
    assert b"Latest comparison scan" not in pending.data
    assert b"court-3" in all_sections.data
    assert "view=pending" in advanced.headers["Location"]
    assert "#change-change-2" in advanced.headers["Location"]

    bulk = client.post(f"/runs/{run_id}/api-changes/approve-all")
    assert "bulk_approved=1" in bulk.headers["Location"]
    bulk_result = client.get(bulk.headers["Location"])
    assert b"1 FaCT change approval(s) recorded" in bulk_result.data
    assert b"court-3" not in bulk_result.data


def test_api_change_refresh_explains_fact_authentication_failure(tmp_path):
    output_root, run_id = _archive(tmp_path)
    request_value = httpx.Request("GET", "http://fact.test/courts/slug/example-court/v1")
    response = httpx.Response(401, request=request_value)
    lookup_error = httpx.HTTPStatusError(
        "Client error '401 Unauthorized'",
        request=request_value,
        response=response,
    )
    service = ApiExecutionService(
        output_root,
        AppConfig(),
        _FakeExecutionClientWithLookupError(lookup_error),
    )
    client = create_app(output_root, execution_service=service).test_client()

    refreshed = client.post(
        f"/runs/{run_id}/api-changes/example-court-1/refresh",
        data={"court_slug": "example-court", "page": "3", "hold": "target_not_checked"},
    )

    assert refreshed.status_code == 400
    assert b"HTTP 401" in refreshed.data
    assert b"Refresh FACT_DATA_API_BEARER_TOKEN" in refreshed.data


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
    assert client.get(f"/runs/{run_id}/courts/missing-court").status_code == 404
    assert client.get(f"/runs/{run_id}/issues?code=UNKNOWN").status_code == 200
    assert client.get(f"/runs/{run_id}/records?q=2&page=not-a-page").status_code == 200
    assert client.get(f"/runs/{run_id}/download/not-in-manifest.json").status_code == 404


def test_operational_submissions_derives_latest_row_and_visible_statuses(tmp_path):
    archive_path = tmp_path / "archive"
    archive_path.mkdir()
    submissions = [
        {
            "source": {
                "source_row_number": 2,
                "completion_time": "2026-07-01T10:00:00Z",
            },
            "court_slug": "duplicate-court",
            "status": "needs_human_review",
                "issues": [
                    {
                        "field": "court_slug",
                        "code": "DUPLICATE_COURT_SLUG",
                    "severity": "warning",
                    "message": "duplicate",
                }
            ],
        },
        {
            "source": {
                "source_row_number": 3,
                "completion_time": "2026-07-02T10:00:00Z",
            },
            "court_slug": "duplicate-court",
            "status": "needs_human_review",
                "issues": [
                    {
                        "field": "court_slug",
                        "code": "DUPLICATE_COURT_SLUG",
                    "severity": "warning",
                    "message": "duplicate",
                }
            ],
        },
    ]
    (archive_path / "submissions_cleaned.json").write_text(json.dumps(submissions))

    operational = _operational_submissions({"path": archive_path})

    assert operational[0]["selection_status"] == "superseded"
    assert operational[0]["superseded_by_source_row_number"] == 3
    assert operational[0]["status"] == "skipped"
    assert operational[1]["selection_status"] == "authoritative"
    assert operational[1]["issues"] == []
    assert operational[1]["status"] == "processed"
    assert _status_from_visible_issues([{"severity": "error"}]) == "failed"
    assert _status_from_visible_issues([{"code": "LLM_LOW_CONFIDENCE"}]) == "needs_human_review"
    assert _status_from_visible_issues([{"severity": "warning"}]) == "processed_with_warnings"

    (archive_path / "submissions_cleaned.json").write_text(json.dumps({"not": "a list"}))
    assert _operational_submissions({"path": archive_path}) == []


def test_workflow_and_review_overview_count_all_hold_types(tmp_path):
    archive_path = tmp_path / "archive"
    archive_path.mkdir()
    (archive_path / "submissions_cleaned.json").write_text(
        json.dumps(
            [
                {
                    "source": {"source_row_number": 2},
                    "court_slug": "example-court",
                    "status": "needs_human_review",
                    "issues": [
                        {
                            "field": "contacts[1].email",
                            "code": "LLM_LOW_CONFIDENCE",
                            "severity": "warning",
                            "message": "review",
                        },
                        {
                            "field": "contacts[1].phone",
                            "code": "INFORMATION_ONLY",
                            "severity": "info",
                            "message": "ignore",
                        },
                    ],
                }
            ]
        )
    )
    archive = {"path": archive_path, "manifest": {"run_id": "run-1"}}
    changes = [
        {
            "change_id": "source",
            "source_row_number": 2,
            "source_selection_required": True,
            "selected_source_row_number": None,
            "comparison": None,
            "action": {"readiness": "pending", "reason": "invalid request"},
            "execution_status": "blocked",
        },
        {
            "change_id": "replacement",
            "source_row_number": None,
            "comparison": {
                "has_existing_data": True,
                "is_no_change": False,
                "merge_conflicts": ["ambiguous type"],
            },
            "target_approved": False,
            "action": {
                "readiness": "pending",
                "reason": "Address verification requires review",
            },
            "execution_status": "unknown",
        },
    ]

    class ReviewService:
        def get_llm_actions_review(self, run_id):
            return {
                "item_count": 1,
                "approval_counts": {"pending": 1},
                "items": [
                    {
                        "review_id": "llm-1",
                        "source_row_number": 2,
                        "approval_status": "pending",
                        "actionable": True,
                    }
                ],
            }

        def get_api_changes_review(self, run_id):
            return {"changes": changes}

        def get_execution_summary(self, run_id):
            return {
                "selected_court_count": 1,
                "court_status_counts": {"pending": 1},
                "action_status_counts": {"blocked": 1},
            }

        def get_cached_execution_summary(self, run_id):
            return {
                "selected_court_count": 1,
                "planned_action_count": 2,
                "court_status_counts": {"pending": 1},
                "action_status_counts": {"blocked": 1},
                "llm_approval_counts": {"total": 1, "pending": 1},
                "review_progress_counts": {
                    "pending_value_decisions": 1,
                    "pending_execution_value_dependencies": 1,
                    "ambiguous_comparisons": 1,
                },
                "replacement_approval_counts": {
                    "comparisons": 1,
                    "required": 1,
                    "approved": 0,
                    "pending": 1,
                    "not_checked": 1,
                },
            }

    service = ReviewService()
    overview = _review_overview(archive, service)
    workflow = _workflow_payload("run-1", service)

    assert overview["needs_review_rows"] == 1
    assert {item["code"] for item in overview["hold_categories"]} >= {
        "llm_approval",
        "source_selection",
        "target_not_checked",
        "target_replacement",
        "invalid_request",
        "os_resolution",
        "execution_attention",
    }
    assert workflow["first_incomplete"] == "llm"
    assert workflow["merge_conflicts"] == 1


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
    app = create_app(output_root, config=AppConfig(), execution_service=execution)
    client = app.test_client()

    detail = client.get(f"/runs/{run_id}/records/2")
    assert b"Refresh this court's live comparisons" in detail.data
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
    assert client.post(
        f"/runs/{run_id}/courts/example-court/execute-safe",
        data={"source_row_number": "2"},
    ).status_code == 403
    assert client.post(f"/runs/{run_id}/execute-safe").status_code == 403


def test_review_ui_executes_a_preflight_safe_action_when_explicitly_enabled(tmp_path, monkeypatch):
    output_root, run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    execution_client = _FakeExecutionClient()
    execution = ApiExecutionService(output_root, AppConfig(), execution_client)
    app = create_app(output_root, config=AppConfig(), execution_service=execution)
    client = app.test_client()

    response = client.post(
        f"/runs/{run_id}/courts/example-court/actions/example-court-1/execute",
        data={"source_row_number": "2"},
    )

    assert response.status_code == 302
    _wait_for_execution_job(app)
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
    app = create_app(output_root, config=AppConfig(), execution_service=execution)
    client = app.test_client()

    success = client.post(
        f"/runs/{run_id}/courts/example-court/execute-safe", data={"source_row_number": "2"}
    )
    assert success.status_code == 302
    _wait_for_execution_job(app)
    assert execution_client.writes

    failing_app = create_app(
        output_root, config=AppConfig(), execution_service=_FailingExecutionService()
    )
    failing_client = failing_app.test_client()
    assert (
        failing_client.post(
            f"/runs/{run_id}/courts/example-court/api-check", data={"source_row_number": "2"}
        ).status_code
        == 400
    )
    for path in [
        f"/runs/{run_id}/courts/example-court/actions/example-court-1/execute",
        f"/runs/{run_id}/courts/example-court/execute-safe",
        f"/runs/{run_id}/execute-safe",
    ]:
        assert failing_client.post(path, data={"source_row_number": "2"}).status_code == 302
        assert _wait_for_execution_job(failing_app).state == "failed"


def test_review_ui_executes_all_safe_actions_for_a_run_and_shows_summary(tmp_path, monkeypatch):
    output_root, run_id = _archive(tmp_path)
    monkeypatch.setenv("FACT_DATA_API_WRITES_ENABLED", "true")
    execution_client = _FakeExecutionClient()
    execution = ApiExecutionService(output_root, AppConfig(), execution_client)
    app = create_app(output_root, config=AppConfig(), execution_service=execution)
    client = app.test_client()

    assert b"Run all available FaCT API actions" in client.get(f"/runs/{run_id}").data
    response = client.post(f"/runs/{run_id}/execute-safe")

    assert response.status_code == 302
    _wait_for_execution_job(app)
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
    assert _load_readiness_report(archive_path)["records"]
    (archive_path / "api_readiness_report.json").unlink()
    (archive_path / "fact_api_import_manifest.json").write_text(json.dumps({"records": []}))
    assert _load_readiness_report(archive_path) == {"records": []}
    client = create_app(output_root).test_client()
    assert client.get("/jobs/missing/status").status_code == 404
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


class _ExistingTargetExecutionClient(_FakeExecutionClient):
    def get(self, path):
        return ApiResponse(200, {"parking": False})


class _FakeExecutionClientWithLookupError(_FakeExecutionClient):
    def __init__(self, lookup_error):
        super().__init__()
        self.lookup_error = lookup_error

    def lookup_court(self, slug):
        raise self.lookup_error


def _wait_for_execution_job(app):
    runner = app.config["EXECUTION_JOB_RUNNER"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        job = runner.active()
        if job is None:
            jobs = list(runner.directory.glob("*.json"))
            assert jobs
            return runner.get(max(jobs, key=lambda path: path.stat().st_mtime).stem)
        time.sleep(0.01)
    raise AssertionError("execution job did not finish")


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
