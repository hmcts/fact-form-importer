import json

from fact_form_importer.execution.approvals import LlmApprovalLedger, LlmDenial
from fact_form_importer.execution.business_report import (
    build_business_report,
    business_report_csv,
    business_report_markdown,
    _theme,
    _write_attempt_counts,
)
from fact_form_importer.execution.models import (
    ActionAttempt,
    ActionExecutionState,
    CourtExecutionState,
    ExecutionLedger,
)
from fact_form_importer.execution.review_state import (
    CourtDisposition,
    ExecutionReviewLedger,
)


def test_business_report_separates_completion_from_sent_write_acceptance():
    ledger = ExecutionLedger(
        run_id="run",
        courts={
            "good-court": CourtExecutionState(
                court_slug="good-court",
                actions={
                    "good": ActionExecutionState(
                        action_id="good",
                        status="succeeded",
                        attempts=[
                            ActionAttempt(
                                operation="execute",
                                outcome="failed",
                                http_status=400,
                                request_duration_ms=40,
                                persistence_duration_ms=10,
                            ),
                            ActionAttempt(
                                operation="execute",
                                outcome="succeeded",
                                http_status=201,
                                message="Completed 2 reviewed merged-section operation(s)",
                                request_duration_ms=120,
                                persistence_duration_ms=20,
                            )
                        ],
                    )
                },
            ),
            "blocked-court": CourtExecutionState(
                court_slug="blocked-court",
                actions={
                    "blocked": ActionExecutionState(
                        action_id="blocked",
                        status="blocked",
                        reason="Multiple contact detail entries use business type 'type-1'",
                        attempts=[
                            ActionAttempt(operation="preflight", outcome="blocked")
                        ],
                    )
                },
            ),
        },
    )
    review = ExecutionReviewLedger(
        run_id="run",
        court_dispositions={
            "4": CourtDisposition(
                source_row_number=4,
                court_slug="missing-court",
                rationale="No matching English or Welsh court exists",
            )
        },
    )
    approvals = LlmApprovalLedger(
        run_id="run",
        denials={
            "review": LlmDenial(
                review_id="review", rationale="The proposed value changes the meaning"
            )
        },
    )

    report = build_business_report("run", ledger, review, approvals)
    markdown = business_report_markdown(report)
    csv_text = business_report_csv(report)

    assert report["action_completion"]["success_percentage"] == 50.0
    assert report["api_write_acceptance"] == {
        "requests_sent": 3,
        "accepted": 2,
        "rejected": 1,
        "uncertain": 0,
        "rejected_or_uncertain": 1,
        "acceptance_percentage": 66.7,
    }
    assert report["timing"] == {
        "api_request_seconds": 0.16,
        "state_persistence_seconds": 0.03,
    }
    assert "Conflicting duplicate business types" in markdown
    assert "No matching English or Welsh court exists" in csv_text
    assert '"body"' not in json.dumps(report).casefold()


def test_business_theme_classification_covers_common_decisions():
    cases = {
        "Explanation exceeds 250 length": "Contact explanations exceed FaCT limits",
        "openingTimesDetails must contain a valid period": "Opening hours cannot be represented safely",
        "email does not match the API format": "Contact details do not meet the FaCT contract",
        "postcode was rejected": "Address or postcode could not be verified",
        "Court does not exist in FaCT": "Court could not be matched in FaCT",
        "LLM approval required": "Reviewer approval remains outstanding",
        "request timed out": "Write outcome is uncertain",
        "required field is invalid": "FaCT request validation",
        "unexpected business issue": "Other blocked or rejected actions",
    }

    assert {_theme(reason)[0] for reason in cases} == set(cases.values())


def test_write_attempt_counts_support_new_metrics_and_legacy_unknown():
    action = ActionExecutionState(
        action_id="action",
        attempts=[
            ActionAttempt(
                operation="execute",
                outcome="succeeded",
                write_request_count=4,
                accepted_write_count=2,
                rejected_write_count=1,
                unknown_write_count=1,
            ),
            ActionAttempt(
                operation="execute",
                outcome="unknown",
                request_duration_ms=50,
            ),
        ],
    )

    assert _write_attempt_counts([("court", action)]) == (2, 1, 2)
