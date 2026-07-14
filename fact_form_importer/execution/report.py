"""Summarise mutable API execution state without changing a run archive."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import re
from typing import Any

from fact_form_importer.execution.approvals import LlmApprovalLedger
from fact_form_importer.execution.models import ExecutionLedger


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
EXECUTION_SUMMARY_VERSION = "1.4"
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
    review_report = review_report or {"items": []}
    approval_ledger = approvals or LlmApprovalLedger(run_id=run_id)
    approved_ids = set(approval_ledger.approvals)

    records = sorted(
        readiness_report.get("records", []),
        key=lambda record: str(record.get("court_slug") or ""),
    )
    for record in records:
        court_slug = str(record.get("court_slug") or "")
        state = ledger.courts.get(court_slug)
        actions: list[dict[str, Any]] = []

        for action in record.get("actions", []):
            action_id = str(action.get("action_id") or "")
            action_state = state.actions.get(action_id) if state else None
            action_status = action_state.status if action_state else "planned"
            if action_status not in {"blocked", "failed", "unknown", "running", "succeeded"}:
                review_ids = _action_review_ids(action, review_report)
                if any(review_id not in approved_ids for review_id in review_ids):
                    action_status = "awaiting_approval"
                elif action_status == "awaiting_approval":
                    action_status = "planned"
            action_counts[action_status] += 1
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
        "common_error_themes": _group_error_themes(attention_actions),
        "attention_by_request_type": _group_attention_by_request_type(attention_actions),
        "attention_actions": attention_actions,
        "courts": courts,
    }


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
    approved = len(actionable_ids & approved_ids)
    auto_approved = sum(
        approval.approval_method == "policy" and review_id in actionable_ids
        for review_id, approval in approvals.approvals.items()
    )
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
        "pending": len(actionable) - approved - already_executed,
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
