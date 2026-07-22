"""Summarise mutable API execution state without changing a run archive."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import re
from typing import Any

from fact_form_importer.execution.approvals import (
    ADDRESS_AUTO_APPROVAL_POLICY_VERSIONS,
    FIELD_AUTO_APPROVAL_POLICY_VERSIONS,
    LEGACY_UNCHANGED_FIELD_AUTO_APPROVAL_POLICY_VERSION,
    LlmApprovalLedger,
)
from fact_form_importer.execution.models import ExecutionLedger
from fact_form_importer.execution.review_state import ExecutionReviewLedger, target_change_id
from fact_form_importer.validators.base import HUMAN_REVIEW_ISSUE_CODES


_ACTION_STATUSES = (
    "planned",
    "awaiting_approval",
    "ready",
    "blocked",
    "running",
    "succeeded",
    "failed",
    "unknown",
)
_COURT_STATUSES = (
    "not_started",
    "awaiting_approval",
    "in_progress",
    "attention_required",
    "completed",
)
_ATTENTION_ACTION_STATUSES = {"blocked", "failed", "unknown"}
EXECUTION_SUMMARY_VERSION = "2.1"
_SOURCE_CORRECTION_CODES = {
    "COURT_SLUG_NOT_FOUND",
    "COURT_SLUG_SUGGESTED",
    "MISSING_COURT_IDENTIFIER",
    "INVALID_POSTCODE",
    "INVALID_TIME",
    "OPENING_HOURS_AMBIGUOUS",
    "VOCAB_NO_MATCH",
    "ADDRESS_OS_REVIEW_REQUIRED",
    "ADDRESS_OS_LOOKUP_UNAVAILABLE",
}
_LLM_INFORMATION_CODES = {
    "LLM_FIELD_NORMALISED",
    "LLM_MODEL_NOTE",
    "LLM_RESPONSE_REVIEW_ADVISORY",
}
_COURT_UUID_IN_PATH = re.compile(
    r"(?<=/courts/)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/)",
    re.IGNORECASE,
)
_RESOURCE_LABELS = {
    "accessibility_options": "Accessibility options",
    "address": "Addresses",
    "building_facilities": "Building facilities",
    "contact_detail": "Contact details",
    "counter_service_opening_hours": "Counter service opening hours",
    "court_opening_hours": "Court opening hours",
    "professional_information": "Professional information",
    "translation_services": "Translation services",
}


def build_execution_summary(
    run_id: str,
    readiness_report: dict[str, Any],
    ledger: ExecutionLedger,
    *,
    review_report: dict[str, Any] | None = None,
    approvals: LlmApprovalLedger | None = None,
    execution_review: ExecutionReviewLedger | None = None,
    submissions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a review-safe summary of all planned court actions.

    The action plan is immutable and may contain request bodies. This summary
    intentionally contains only operational identifiers, status and diagnostic
    text so it can be used in the UI and shared as a failure report.
    """

    action_counts: Counter[str] = Counter()
    court_counts: Counter[str] = Counter()
    courts: list[dict[str, Any]] = []
    attention_actions: list[dict[str, Any]] = []
    succeeded_action_ids: set[str] = set()
    hold_courts: dict[str, set[str]] = defaultdict(set)
    review_report = review_report or {"items": []}
    approval_ledger = approvals or LlmApprovalLedger(run_id=run_id)
    approved_ids = set(approval_ledger.approvals)
    denied_ids = set(approval_ledger.denials)
    review_ledger = execution_review or ExecutionReviewLedger(run_id=run_id)

    records = sorted(
        [
            record
            for record in readiness_report.get("records", [])
            if not _record_is_disposed(record, review_ledger, ledger)
        ],
        key=lambda record: str(record.get("court_slug") or ""),
    )
    for record in records:
        court_slug = str(record.get("court_slug") or "")
        state = ledger.courts.get(court_slug)
        actions: list[dict[str, Any]] = []

        for action in record.get("actions", []):
            if action.get("source_selection_required"):
                selection = review_ledger.source_selections.get(court_slug)
                if selection and selection.source_row_number != action.get("source_row_number"):
                    continue
            action_id = str(action.get("action_id") or "")
            action_state = state.actions.get(action_id) if state else None
            action_status = action_state.status if action_state else "planned"
            if action_status not in {"blocked", "failed", "unknown", "running", "succeeded"}:
                hold_codes = []
                if action.get("source_selection_required") and not review_ledger.source_selections.get(court_slug):
                    hold_codes.append("source_selection")
                review_ids = _action_review_ids(action, review_report)
                if any(review_id not in approved_ids for review_id in review_ids):
                    hold_codes.append("value_approval")
                if any(review_id in denied_ids for review_id in review_ids):
                    hold_codes.append("denied_value")
                comparison = review_ledger.comparisons.get(
                    target_change_id(court_slug, action_id)
                )
                if comparison and comparison.has_existing_data and not comparison.is_no_change:
                    if comparison.change_id not in review_ledger.target_approvals:
                        hold_codes.append("target_replacement")
                if hold_codes:
                    action_status = "awaiting_approval"
                elif action_status == "awaiting_approval":
                    action_status = "planned"
            else:
                hold_codes = []
            action_counts[action_status] += 1
            for hold_code in hold_codes:
                hold_courts[hold_code].add(court_slug)
            if action_status == "succeeded":
                succeeded_action_ids.add(action_id)
            action_result = {
                "action_id": action_id,
                "resource": action.get("resource"),
                "method": action.get("method"),
                "path": action.get("path"),
                "plan_readiness": action.get("readiness"),
                "status": action_status,
                "http_status": action_state.last_response_status if action_state else None,
                "reason": action_state.reason if action_state else action.get("reason"),
                "hold_codes": hold_codes,
            }
            actions.append(action_result)
            if action_status in _ATTENTION_ACTION_STATUSES:
                attention_actions.append(
                    {
                        "court_slug": court_slug,
                        "source_row_numbers": record.get("source_row_numbers", []),
                        **action_result,
                    }
                )

        court_status = _summary_court_status(actions)
        court_counts[court_status] += 1
        courts.append(
            {
                "court_slug": court_slug,
                "court_id": state.court_id if state else record.get("court_id"),
                "source_row_numbers": record.get("source_row_numbers", []),
                "status": court_status,
                "actions": actions,
            }
        )

    replacement_counts = _replacement_approval_counts(
        review_ledger, sum(action_counts.values())
    )
    review_progress = _review_progress_counts(
        submissions or [],
        review_report,
        approval_ledger,
        readiness_report,
        review_ledger,
    )
    approval_hold_courts = hold_courts["value_approval"] | hold_courts[
        "target_replacement"
    ] | hold_courts["source_selection"]
    return {
        "summary_version": EXECUTION_SUMMARY_VERSION,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selected_court_count": len(records),
        "planned_action_count": sum(action_counts.values()),
        "court_status_counts": {status: court_counts[status] for status in _COURT_STATUSES},
        "action_status_counts": {status: action_counts[status] for status in _ACTION_STATUSES},
        "attention_action_count": len(attention_actions),
        "llm_approval_counts": _llm_approval_counts(
            review_report, approval_ledger, succeeded_action_ids
        ),
        "review_progress_counts": review_progress,
        "court_hold_counts": {
            "value_approval": len(hold_courts["value_approval"]),
            "denied_value": len(hold_courts["denied_value"]),
            "target_replacement": len(hold_courts["target_replacement"]),
            "source_selection": len(hold_courts["source_selection"]),
            "held_by_approvals": len(approval_hold_courts),
            "without_known_approval_hold": len(records) - len(approval_hold_courts),
        },
        "replacement_approval_counts": replacement_counts,
        "duplicate_source_selection_counts": {
            "selected": len(review_ledger.source_selections),
            "pending_courts": sum(
                any(action.get("source_selection_required") for action in record.get("actions", []))
                and str(record.get("court_slug") or "") not in review_ledger.source_selections
                for record in records
            ),
        },
        "common_error_themes": _group_error_themes(attention_actions),
        "attention_by_request_type": _group_attention_by_request_type(attention_actions),
        "attention_actions": attention_actions,
        "courts": courts,
    }


def _replacement_approval_counts(
    review: ExecutionReviewLedger, planned_action_count: int
) -> dict[str, int]:
    required = [
        comparison
        for comparison in review.comparisons.values()
        if comparison.has_existing_data and not comparison.is_no_change
    ]
    approved = sum(
        comparison.change_id in review.target_approvals for comparison in required
    )
    return {
        "comparisons": len(review.comparisons),
        "required": len(required),
        "approved": approved,
        "pending": len(required) - approved,
        "not_checked": max(planned_action_count - len(review.comparisons), 0),
    }


def _review_progress_counts(
    submissions: list[dict[str, Any]],
    review_report: dict[str, Any],
    approvals: LlmApprovalLedger,
    readiness_report: dict[str, Any],
    execution_review: ExecutionReviewLedger,
) -> dict[str, int]:
    """Count known outstanding human work once per authoritative court identity."""

    approved_ids = set(approvals.approvals)
    denied_ids = set(approvals.denials)
    disposed_rows = {
        int(row)
        for row in execution_review.court_dispositions
        if str(row).isdigit()
    }
    rows = {
        submission.get("source", {}).get("source_row_number"): submission
        for submission in submissions
    }
    ingestion_review_courts = {
        _submission_court_key(submission)
        for submission in submissions
        if submission.get("status") == "needs_human_review"
    }
    review_items_by_row: dict[int, list[dict[str, Any]]] = defaultdict(list)
    pending_value_courts: set[str] = set()
    pending_execution_value_courts: set[str] = set()
    denied_value_courts: set[str] = set()
    pending_value_items = []
    pending_execution_value_items = []
    denied_value_items = []
    for item in review_report.get("items", []):
        review_id = str(item.get("review_id") or "")
        if not review_id or not item.get("approvable", item.get("actionable")):
            continue
        row = item.get("source_row_number")
        if row in disposed_rows:
            continue
        if isinstance(row, int):
            review_items_by_row[row].append(item)
        if review_id in approved_ids:
            continue
        if review_id in denied_ids:
            denied_value_items.append(item)
            denied_value_courts.add(_review_court_key(item, rows.get(row)))
            continue
        pending_value_items.append(item)
        court_key = _review_court_key(item, rows.get(row))
        pending_value_courts.add(court_key)
        if item.get("actionable"):
            pending_execution_value_items.append(item)
            pending_execution_value_courts.add(court_key)

    source_correction_courts: set[str] = set()
    source_correction_items = 0
    for submission in submissions:
        row = submission.get("source", {}).get("source_row_number")
        if row in disposed_rows:
            continue
        row_items = review_items_by_row.get(row, []) if isinstance(row, int) else []
        for issue in submission.get("issues", []):
            if (
                isinstance(row, int)
                and str(row) in execution_review.court_target_overrides
                and issue.get("code")
                in {"COURT_SLUG_NOT_FOUND", "COURT_SLUG_SUGGESTED"}
            ):
                continue
            if _issue_is_covered_by_review_item(issue, row_items):
                continue
            if not _issue_needs_source_correction(issue):
                continue
            source_correction_items += 1
            source_correction_courts.add(_submission_court_key(submission))

    existing_data_courts: set[str] = set()
    ambiguous_courts: set[str] = set()
    existing_data_items = 0
    ambiguous_items = 0
    for comparison in execution_review.comparisons.values():
        if comparison.merge_conflicts:
            ambiguous_items += 1
            ambiguous_courts.add(comparison.court_slug)
        if (
            comparison.has_existing_data
            and not comparison.is_no_change
            and not comparison.merge_conflicts
            and comparison.change_id not in execution_review.target_approvals
        ):
            existing_data_items += 1
            existing_data_courts.add(comparison.court_slug)

    invalid_section_courts: set[str] = set()
    invalid_section_items = 0
    for record in readiness_report.get("records", []):
        if any(row in disposed_rows for row in record.get("source_row_numbers", [])):
            continue
        court_slug = str(record.get("court_slug") or "")
        for action in record.get("actions", []):
            if action.get("readiness") != "pending":
                continue
            invalid_section_items += 1
            invalid_section_courts.add(court_slug or _record_court_key(record))

    source_or_value = pending_value_courts | source_correction_courts
    outstanding = (
        source_or_value
        | existing_data_courts
        | ambiguous_courts
        | invalid_section_courts
    )
    return {
        "unique_courts_outstanding": len(outstanding),
        "pending_value_decisions": len(pending_value_items),
        "pending_value_decision_courts": len(pending_value_courts),
        "pending_execution_value_dependencies": len(
            pending_execution_value_items
        ),
        "pending_execution_value_dependency_courts": len(
            pending_execution_value_courts
        ),
        "denied_value_decisions": len(denied_value_items),
        "denied_value_decision_courts": len(denied_value_courts),
        "ingestion_review_courts": len(ingestion_review_courts),
        "ingestion_or_value_review_courts": len(
            ingestion_review_courts | pending_value_courts
        ),
        "source_or_value_review_courts": len(source_or_value),
        "source_correction_items": source_correction_items,
        "source_correction_courts": len(source_correction_courts),
        "existing_data_approvals_pending": existing_data_items,
        "existing_data_approval_courts": len(existing_data_courts),
        "ambiguous_comparisons": ambiguous_items,
        "ambiguous_comparison_courts": len(ambiguous_courts),
        "invalid_section_actions": invalid_section_items,
        "invalid_section_courts": len(invalid_section_courts),
    }


def _record_is_disposed(
    record: dict[str, Any],
    review: ExecutionReviewLedger,
    ledger: ExecutionLedger,
) -> bool:
    rows = record.get("source_row_numbers", [])
    if not rows or not all(str(row) in review.court_dispositions for row in rows):
        return False
    state = ledger.courts.get(str(record.get("court_slug") or ""))
    return not state or not any(
        action.status == "succeeded" for action in state.actions.values()
    )


def _submission_court_key(submission: dict[str, Any]) -> str:
    slug = str(submission.get("court_slug") or "").strip()
    row = submission.get("source", {}).get("source_row_number")
    return slug or f"source-row-{row}"


def _record_court_key(record: dict[str, Any]) -> str:
    slug = str(record.get("court_slug") or "").strip()
    rows = record.get("source_row_numbers", [])
    return slug or f"source-row-{rows[0] if rows else 'unknown'}"


def _review_court_key(
    item: dict[str, Any], submission: dict[str, Any] | None
) -> str:
    slug = str(item.get("court_slug") or "").strip()
    if slug:
        return slug
    if submission:
        return _submission_court_key(submission)
    return f"source-row-{item.get('source_row_number')}"


def _issue_is_covered_by_review_item(
    issue: dict[str, Any], items: list[dict[str, Any]]
) -> bool:
    code = str(issue.get("code") or "")
    if not (code.startswith("LLM_") or code.startswith("ADDRESS_OS_")):
        return False
    kind = "address" if code.startswith("ADDRESS_OS_") else "field"
    issue_field = str(issue.get("field") or "")
    candidates = [item for item in items if item.get("kind") == kind]
    overlapping = any(
        _review_fields_overlap(issue_field, str(item.get("field") or ""))
        for item in candidates
    )
    return overlapping or bool(candidates)


def _review_fields_overlap(left: str, right: str) -> bool:
    return bool(left and right) and (
        left == right
        or left.startswith(right + ".")
        or left.startswith(right + "[")
        or right.startswith(left + ".")
        or right.startswith(left + "[")
    )


def _issue_needs_source_correction(issue: dict[str, Any]) -> bool:
    code = str(issue.get("code") or "")
    if code in _LLM_INFORMATION_CODES:
        return False
    return bool(
        code in _SOURCE_CORRECTION_CODES
        or code in HUMAN_REVIEW_ISSUE_CODES
        or issue.get("severity") == "error"
    )


def _action_review_ids(action: dict[str, Any], review_report: dict[str, Any]) -> set[str]:
    action_id = str(action.get("action_id") or "")
    explicit = {str(value) for value in action.get("llm_review_ids", []) if value}
    derived = {
        str(item["review_id"])
        for item in review_report.get("items", [])
        if item.get("actionable")
        and action_id in item.get("dependent_action_ids", [])
        and item.get("review_id")
    }
    return explicit | derived


def _summary_court_status(actions: list[dict[str, Any]]) -> str:
    statuses = [str(action.get("status") or "planned") for action in actions]
    if statuses and all(status == "succeeded" for status in statuses):
        return "completed"
    if any(status in _ATTENTION_ACTION_STATUSES for status in statuses):
        return "attention_required"
    if any(status == "awaiting_approval" for status in statuses):
        return "awaiting_approval"
    if any(status != "planned" for status in statuses):
        return "in_progress"
    return "not_started"


def _llm_approval_counts(
    review_report: dict[str, Any],
    approvals: LlmApprovalLedger,
    succeeded_action_ids: set[str],
) -> dict[str, int]:
    actionable = [item for item in review_report.get("items", []) if item.get("actionable")]
    actionable_ids = {str(item.get("review_id")) for item in actionable}
    approved_ids = set(approvals.approvals)
    denied_ids = set(approvals.denials)
    approved = len(actionable_ids & approved_ids)
    denied = len(actionable_ids & denied_ids)
    auto_approved = sum(
        approval.approval_method == "policy" and review_id in actionable_ids
        for review_id, approval in approvals.approvals.items()
    )
    policy_approved = {
        review_id: approval
        for review_id, approval in approvals.approvals.items()
        if approval.approval_method == "policy"
    }
    already_executed = sum(
        str(item.get("review_id")) not in approved_ids
        and bool(item.get("dependent_action_ids"))
        and all(
            str(action_id) in succeeded_action_ids
            for action_id in item.get("dependent_action_ids", [])
        )
        for item in actionable
    )
    return {
        "total": len(actionable),
        "approved": approved,
        "manual_approved": approved - auto_approved,
        "auto_approved": auto_approved,
        "auto_approved_total": len(policy_approved),
        "auto_approved_addresses": sum(
            approval.policy_version in ADDRESS_AUTO_APPROVAL_POLICY_VERSIONS
            for approval in policy_approved.values()
        ),
        "auto_approved_unchanged_fields": sum(
            approval.policy_version
            == LEGACY_UNCHANGED_FIELD_AUTO_APPROVAL_POLICY_VERSION
            for approval in policy_approved.values()
        ),
        "auto_approved_fields": sum(
            approval.policy_version in FIELD_AUTO_APPROVAL_POLICY_VERSIONS
            for approval in policy_approved.values()
        ),
        "denied": denied,
        "pending": len(actionable) - approved - denied - already_executed,
        "already_executed": already_executed,
        "not_actionable": sum(
            not item.get("actionable") for item in review_report.get("items", [])
        ),
    }


def _group_attention_by_request_type(
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group attention outcomes into a product-decision friendly report."""

    resources: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        resources[str(action.get("resource") or "unknown")].append(action)

    grouped_resources = []
    for resource, resource_actions in resources.items():
        outcome_groups: dict[tuple[str, Any, str], list[dict[str, Any]]] = defaultdict(list)
        for action in resource_actions:
            key = (
                str(action.get("status") or "unknown"),
                action.get("http_status"),
                _normalised_attention_reason(str(action.get("reason") or "")),
            )
            outcome_groups[key].append(action)

        outcomes = []
        for (status, http_status, reason), outcome_actions in outcome_groups.items():
            courts = sorted({str(action["court_slug"]) for action in outcome_actions})
            classification = _attention_classification(resource, status, http_status, reason)
            outcomes.append(
                {
                    "classification": classification,
                    "status": status,
                    "http_status": http_status,
                    "reason": reason,
                    "action_count": len(outcome_actions),
                    "court_count": len(courts),
                    "example_courts": courts[:5],
                    "example_reason": next(
                        (
                            str(action["reason"])
                            for action in outcome_actions
                            if action.get("reason")
                        ),
                        None,
                    ),
                    "decision_guidance": _decision_guidance(resource, classification),
                }
            )

        status_counts = Counter(
            str(action.get("status") or "unknown") for action in resource_actions
        )
        courts = {str(action["court_slug"]) for action in resource_actions}
        grouped_resources.append(
            {
                "resource": resource,
                "label": _RESOURCE_LABELS.get(resource, resource.replace("_", " ").title()),
                "methods": sorted(
                    {
                        str(action.get("method"))
                        for action in resource_actions
                        if action.get("method")
                    }
                ),
                "endpoint_templates": sorted(
                    {
                        _COURT_UUID_IN_PATH.sub("{court_id}", str(action.get("path") or ""))
                        for action in resource_actions
                        if action.get("path")
                    }
                ),
                "attention_action_count": len(resource_actions),
                "court_count": len(courts),
                "status_counts": {
                    status: status_counts[status] for status in ("blocked", "failed", "unknown")
                },
                "distinct_outcome_count": len(outcomes),
                "outcomes": sorted(
                    outcomes,
                    key=lambda outcome: (
                        -outcome["action_count"],
                        outcome["status"],
                        outcome["reason"],
                    ),
                ),
            }
        )

    return sorted(
        grouped_resources,
        key=lambda group: (-group["attention_action_count"], group["resource"]),
    )


def _normalised_attention_reason(reason: str) -> str:
    if reason.startswith(
        "Address verification requires review: FaCT/Ordnance Survey returned no address result:"
    ):
        return (
            "Address verification requires review: FaCT/Ordnance Survey returned no address "
            "result for the submitted postcode"
        )
    return reason or "No diagnostic reason was recorded"


def _attention_classification(resource: str, status: str, http_status: Any, reason: str) -> str:
    if "Target section already contains FaCT data" in reason:
        return "expected_no_overwrite"
    if resource == "address" and "Address verification" in reason:
        return "address_review"
    if status == "failed" or isinstance(http_status, int) and http_status >= 400:
        return "api_rejection"
    if status == "unknown":
        return "execution_uncertain"
    if resource in {"accessibility_options", "professional_information"}:
        return "missing_or_invalid_form_data"
    if resource in {
        "contact_detail",
        "counter_service_opening_hours",
        "court_opening_hours",
    }:
        return "invalid_form_data"
    return "human_review"


def _decision_guidance(resource: str, classification: str) -> str:
    if classification == "expected_no_overwrite":
        return (
            "Confirm that the earlier migration owns this section. The importer deliberately "
            "does not merge with or overwrite existing FaCT data."
        )
    if classification == "address_review":
        return (
            "Review the submitted address against FaCT/Ordnance Survey evidence. Do not write "
            "an unresolved or unsupported address automatically."
        )
    if classification == "api_rejection":
        return (
            "Inspect the rejected value or contract mismatch, correct the importer/source rule, "
            "then generate a new run before retrying."
        )
    if classification == "execution_uncertain":
        return "Resolve authentication, connectivity or timeout uncertainty before retrying."
    if resource == "accessibility_options":
        return (
            "Convert valid measurements deterministically where possible. Use numeric defaults "
            "only for genuinely blank dependent values; never invent a public phone number."
        )
    if resource == "professional_information":
        return (
            "Review the submitted room count. A value of 1 is only safe when interview rooms are "
            "Yes and the dependent count is genuinely blank."
        )
    if resource in {"counter_service_opening_hours", "court_opening_hours"}:
        return (
            "Review the source hours and correct them in the form/admin workflow when they cannot "
            "be represented as a valid opening period."
        )
    if resource == "contact_detail":
        return (
            "Use a verified public phone number or email, or omit the contact after review. Do not "
            "invent public contact details."
        )
    return "Review the source evidence and FaCT contract before deciding whether to write."


def _group_error_themes(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "courts": set(), "example_reason": None}
    )
    for action in actions:
        code, label = _error_theme(
            action.get("status"), action.get("http_status"), action.get("reason")
        )
        group = groups[code]
        group["count"] += 1
        group["courts"].add(action["court_slug"])
        group["label"] = label
        if group["example_reason"] is None and action.get("reason"):
            group["example_reason"] = action["reason"]

    return [
        {
            "code": code,
            "label": group["label"],
            "action_count": group["count"],
            "court_count": len(group["courts"]),
            "courts": sorted(group["courts"]),
            "example_reason": group["example_reason"],
        }
        for code, group in sorted(groups.items(), key=lambda item: (-item[1]["count"], item[0]))
    ]


def _error_theme(status: Any, http_status: Any, reason: Any) -> tuple[str, str]:
    message = str(reason or "")
    if "Target section already contains FaCT data" in message:
        return "target_has_existing_data", "Target section already contains FaCT data"
    if "Court does not exist in FaCT" in message:
        return "court_missing", "Court no longer exists in FaCT"
    if "Court UUID no longer matches" in message:
        return "court_uuid_mismatch", "Court UUID changed since the reviewed run"
    if (
        "Address verification" in message
        or "Ordnance Survey" in message
        or "postcode lookup" in message
    ):
        return "address_verification", "Address verification prevented the write"
    if any(
        field in message
        for field in (
            "liftDoorWidth",
            "liftDoorLimit",
            "liftSupportPhoneNumber",
            "accessibleEntrancePhoneNumber",
        )
    ):
        return (
            "missing_accessibility_detail",
            "Form data is missing an accessibility detail required by FaCT",
        )
    if "professionalInformation.interviewRoomCount" in message:
        return (
            "invalid_interview_room_detail",
            "Interview-room data is inconsistent or incomplete for FaCT",
        )
    if "openingTimesDetails" in message:
        return "invalid_opening_hours", "Opening-hours data cannot be represented safely in FaCT"
    if "phoneNumber does not match" in message or "email does not match" in message:
        return "invalid_contact_detail", "Contact data does not meet the FaCT API format"
    if "Action body cannot be sent" in message:
        return "body_validation", "Action body does not meet the FaCT API contract"
    if status == "unknown" and "timed out" in message:
        return "write_timeout", "Write outcome is unknown after a timeout"
    if http_status == 401:
        return "unauthorised", "FaCT API authentication failed"
    if http_status == 403:
        return "forbidden", "FaCT API rejected this account or permission"
    if isinstance(http_status, int) and http_status == 400:
        return "api_validation", "FaCT API rejected the request body"
    if isinstance(http_status, int) and http_status >= 500:
        return "api_unavailable", "FaCT API returned a server error"
    if "Could not connect to FaCT API" in message:
        return "api_unavailable", "FaCT API could not be reached"
    if "Unexpected execution error" in message:
        return "unexpected_execution_error", "Unexpected importer execution error"
    return "other_attention_required", "Action requires human attention"
