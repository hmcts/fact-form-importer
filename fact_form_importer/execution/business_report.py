"""Business-facing outcomes without request bodies or raw submitted data."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from fact_form_importer.execution.approvals import LlmApprovalLedger
from fact_form_importer.execution.models import ExecutionLedger
from fact_form_importer.execution.review_state import ExecutionReviewLedger


def build_business_report(
    run_id: str,
    ledger: ExecutionLedger,
    review: ExecutionReviewLedger,
    approvals: LlmApprovalLedger,
) -> dict[str, Any]:
    actions = [
        (court_slug, action)
        for court_slug, court in ledger.courts.items()
        for action in court.actions.values()
    ]
    action_counts = {
        status: sum(action.status == status for _, action in actions)
        for status in (
            "planned",
            "awaiting_approval",
            "ready",
            "blocked",
            "running",
            "succeeded",
            "failed",
            "unknown",
        )
    }
    completed = sum(
        action_counts[status] for status in ("succeeded", "blocked", "failed", "unknown")
    )
    accepted, rejected, uncertain = _write_attempt_counts(actions)
    sent = accepted + rejected + uncertain
    themes: dict[str, dict[str, Any]] = {}
    for court_slug, action in actions:
        if action.status not in {"blocked", "failed", "unknown"}:
            continue
        theme, recommendation = _theme(action.reason or "")
        value = themes.setdefault(
            theme,
            {
                "theme": theme,
                "action_count": 0,
                "courts": set(),
                "examples": [],
                "reviewer_rationales": [],
                "recommended_next_decision": recommendation,
            },
        )
        value["action_count"] += 1
        value["courts"].add(court_slug)
        if len(value["examples"]) < 5 and court_slug not in value["examples"]:
            value["examples"].append(court_slug)
    for denial in approvals.denials.values():
        _append_decision_theme(themes, "Reviewer-denied model values", denial.rationale)
    for resolution in review.collection_item_resolutions.values():
        if resolution.decision == "omit":
            _append_decision_theme(
                themes,
                "Reviewer-omitted duplicate entries",
                resolution.rationale,
                court_slug=resolution.action_id.rsplit("-", 1)[0],
            )
    for disposition in review.court_dispositions.values():
        _append_decision_theme(
            themes,
            "Courts closed as unactionable",
            disposition.rationale,
            court_slug=disposition.court_slug,
        )
    theme_rows = []
    for value in themes.values():
        courts = sorted(value.pop("courts"))
        value["unique_court_count"] = len(courts)
        theme_rows.append(value)
    theme_rows.sort(key=lambda row: (-row["action_count"], row["theme"]))
    request_ms = sum(
        attempt.request_duration_ms or 0
        for _, action in actions
        for attempt in action.attempts
    )
    persistence_ms = sum(
        attempt.persistence_duration_ms or 0
        for _, action in actions
        for attempt in action.attempts
    )
    return {
        "report_version": "1.1",
        "run_id": run_id,
        "action_completion": {
            **action_counts,
            "completed_terminal_actions": completed,
            "success_percentage": _percentage(action_counts["succeeded"], completed),
        },
        "api_write_acceptance": {
            "requests_sent": sent,
            "accepted": accepted,
            "rejected": rejected,
            "uncertain": uncertain,
            "rejected_or_uncertain": rejected + uncertain,
            "acceptance_percentage": _percentage(accepted, sent),
        },
        "timing": {
            "api_request_seconds": round(request_ms / 1000, 3),
            "state_persistence_seconds": round(persistence_ms / 1000, 3),
        },
        "themes": theme_rows,
    }


def business_report_markdown(report: dict[str, Any]) -> str:
    completion = report["action_completion"]
    acceptance = report["api_write_acceptance"]
    lines = [
        f"# FaCT importer outcome report — {report['run_id']}",
        "",
        "## Outcome",
        "",
        f"- Action completion success: {completion['succeeded']} of {completion['completed_terminal_actions']} ({completion['success_percentage']}%).",
        f"- API write acceptance: {acceptance['accepted']} of {acceptance['requests_sent']} requests sent ({acceptance['acceptance_percentage']}%).",
        f"- API writes rejected: {acceptance['rejected']}; uncertain write outcomes: {acceptance['uncertain']}.",
        f"- Blocked: {completion['blocked']}; failed: {completion['failed']}; unknown: {completion['unknown']}.",
        "",
        "## Common themes",
        "",
    ]
    for theme in report["themes"]:
        examples = ", ".join(theme["examples"]) or "None recorded"
        lines.extend(
            [
                f"### {theme['theme']}",
                "",
                f"- Actions: {theme['action_count']}; unique courts: {theme['unique_court_count']}.",
                f"- Examples: {examples}.",
                f"- Recommended next decision: {theme['recommended_next_decision']}",
            ]
        )
        if theme["reviewer_rationales"]:
            lines.append(
                "- Reviewer rationale: " + "; ".join(theme["reviewer_rationales"][:5])
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def business_report_csv(report: dict[str, Any]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=(
            "theme",
            "action_count",
            "unique_court_count",
            "example_courts",
            "reviewer_rationales",
            "recommended_next_decision",
        ),
    )
    writer.writeheader()
    for theme in report["themes"]:
        writer.writerow(
            {
                "theme": theme["theme"],
                "action_count": theme["action_count"],
                "unique_court_count": theme["unique_court_count"],
                "example_courts": "; ".join(theme["examples"]),
                "reviewer_rationales": "; ".join(theme["reviewer_rationales"]),
                "recommended_next_decision": theme["recommended_next_decision"],
            }
        )
    return output.getvalue()


def _append_decision_theme(
    themes: dict[str, dict[str, Any]],
    theme: str,
    rationale: str,
    *,
    court_slug: str | None = None,
) -> None:
    value = themes.setdefault(
        theme,
        {
            "theme": theme,
            "action_count": 0,
            "courts": set(),
            "examples": [],
            "reviewer_rationales": [],
            "recommended_next_decision": "Use the recorded reviewer rationale as the audit decision.",
        },
    )
    value["action_count"] += 1
    if court_slug:
        value["courts"].add(court_slug)
        if len(value["examples"]) < 5 and court_slug not in value["examples"]:
            value["examples"].append(court_slug)
    if rationale and rationale not in value["reviewer_rationales"]:
        value["reviewer_rationales"].append(rationale)


def _write_attempt_counts(
    actions: list[tuple[str, Any]],
) -> tuple[int, int, int]:
    """Count HTTP mutations, including rejected attempts later retried successfully."""

    accepted = rejected = uncertain = 0
    completed_pattern = re.compile(r"Completed (\d+) reviewed merged-section operation")
    for _, action in actions:
        for attempt in action.attempts:
            if attempt.operation != "execute":
                continue
            if attempt.write_request_count is not None:
                accepted += attempt.accepted_write_count or 0
                rejected += attempt.rejected_write_count or 0
                uncertain += attempt.unknown_write_count or 0
                continue
            # Legacy ledgers recorded one terminal attempt for the whole logical
            # action. Successful collection actions include their operation
            # count in the audit message, allowing exact reconstruction.
            if attempt.outcome == "succeeded":
                match = completed_pattern.search(attempt.message or "")
                accepted += int(match.group(1)) if match else 1
            elif attempt.outcome == "failed":
                rejected += 1
            elif attempt.outcome == "unknown" and (attempt.request_duration_ms or 0) > 0:
                uncertain += 1
    return accepted, rejected, uncertain


def _theme(reason: str) -> tuple[str, str]:
    lowered = reason.casefold()
    if "entries use business type" in lowered or "ambiguous" in lowered:
        return (
            "Conflicting duplicate business types",
            "Ask the business to retain, remap or omit each conflicting entry with a reason.",
        )
    if "250" in lowered or "explanation" in lowered and "length" in lowered:
        return (
            "Contact explanations exceed FaCT limits",
            "Approve a factual explanation of at most 250 characters or omit the optional value.",
        )
    if (
        "openingtimesdetails" in lowered
        or "opening time" in lowered
        or "closing time" in lowered
    ):
        return (
            "Opening hours cannot be represented safely",
            "Correct the submitted periods or confirm that the section should be omitted; do not invent opening hours.",
        )
    if "email" in lowered or "phone" in lowered:
        return (
            "Contact details do not meet the FaCT contract",
            "Correct the contact value or explicitly omit the optional entry with a reviewer reason.",
        )
    if "postcode" in lowered or "ordnance survey" in lowered:
        return ("Address or postcode could not be verified", "Correct the source address and rerun.")
    if "court does not exist" in lowered or "court target" in lowered:
        return (
            "Court could not be matched in FaCT",
            "Select a defensible existing court or close the submission as unactionable with a reason.",
        )
    if "approval" in lowered:
        return ("Reviewer approval remains outstanding", "Complete the linked review decision.")
    if "timed out" in lowered or "unknown" in lowered:
        return (
            "Write outcome is uncertain",
            "Inspect live FaCT state before deciding whether another write is safe.",
        )
    if "required" in lowered or "invalid" in lowered or "cannot be sent" in lowered:
        return ("FaCT request validation", "Correct or explicitly omit the invalid submitted value.")
    return ("Other blocked or rejected actions", "Review the court workspace evidence and decide whether source correction is possible.")


def _percentage(numerator: int, denominator: int) -> float:
    return round((numerator / denominator * 100) if denominator else 0.0, 1)
