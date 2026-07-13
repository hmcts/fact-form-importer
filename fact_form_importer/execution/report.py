"""Summarise mutable API execution state without changing a run archive."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from fact_form_importer.execution.models import ExecutionLedger


_ACTION_STATUSES = (
    "planned",
    "ready",
    "blocked",
    "running",
    "succeeded",
    "failed",
    "unknown",
)
_COURT_STATUSES = ("not_started", "in_progress", "attention_required", "completed")
_ATTENTION_ACTION_STATUSES = {"blocked", "failed", "unknown"}


def build_execution_summary(
    run_id: str,
    readiness_report: dict[str, Any],
    ledger: ExecutionLedger,
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

    records = sorted(
        readiness_report.get("records", []),
        key=lambda record: str(record.get("court_slug") or ""),
    )
    for record in records:
        court_slug = str(record.get("court_slug") or "")
        state = ledger.courts.get(court_slug)
        court_status = state.status if state else "not_started"
        court_counts[court_status] += 1
        actions: list[dict[str, Any]] = []

        for action in record.get("actions", []):
            action_id = str(action.get("action_id") or "")
            action_state = state.actions.get(action_id) if state else None
            action_status = action_state.status if action_state else "planned"
            action_counts[action_status] += 1
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
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selected_court_count": len(records),
        "planned_action_count": sum(action_counts.values()),
        "court_status_counts": {status: court_counts[status] for status in _COURT_STATUSES},
        "action_status_counts": {status: action_counts[status] for status in _ACTION_STATUSES},
        "attention_action_count": len(attention_actions),
        "common_error_themes": _group_error_themes(attention_actions),
        "attention_actions": attention_actions,
        "courts": courts,
    }


def _group_error_themes(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "courts": set(), "example_reason": None}
    )
    for action in actions:
        code, label = _error_theme(action.get("status"), action.get("http_status"), action.get("reason"))
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
        for code, group in sorted(
            groups.items(), key=lambda item: (-item[1]["count"], item[0])
        )
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
