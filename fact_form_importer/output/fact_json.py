"""Draft FaCT JSON payload generation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from fact_form_importer.models.court_submission import CourtSubmission

IMPORTABLE_STATUSES = {"processed", "processed_with_warnings"}


def build_fact_payload(submissions: list[CourtSubmission]) -> list[dict[str, Any]]:
    """Build an inspectable draft payload for records that can be imported.

    This is deliberately not the final FaCT API request shape. It keeps the
    cleaned submission structure visible while later tasks refine mappings into
    endpoint-specific payloads.
    """

    return [
        _payload_record(submission)
        for submission in submissions
        if submission.status in IMPORTABLE_STATUSES and not _has_blocking_issue(submission)
    ]


def build_failed_records(submissions: list[CourtSubmission]) -> list[dict[str, Any]]:
    return [
        _review_record(submission)
        for submission in submissions
        if submission.status == "failed"
    ]


def build_human_review_records(submissions: list[CourtSubmission]) -> list[dict[str, Any]]:
    return [
        _review_record(submission)
        for submission in submissions
        if submission.status == "needs_human_review"
    ]


def _payload_record(submission: CourtSubmission) -> dict[str, Any]:
    return {
        "court_slug": submission.court_slug,
        "source_row_number": submission.source.source_row_number,
        "status": submission.status,
        "facilities": submission.facilities,
        "translation_phone": submission.translation_phone,
        "translation_email": submission.translation_email,
        "addresses": [address.model_dump(mode="json") for address in submission.addresses],
        "counter_service": _json_safe(submission.counter_service),
        "interview_rooms": _json_safe(submission.interview_rooms),
        "contacts": [contact.model_dump(mode="json") for contact in submission.contacts],
        "opening_hours": [hours.model_dump(mode="json") for hours in submission.opening_hours],
        "issues": [issue.model_dump(mode="json") for issue in submission.issues],
    }


def _review_record(submission: CourtSubmission) -> dict[str, Any]:
    return {
        "source": submission.source.model_dump(mode="json"),
        "court_slug_raw": submission.court_slug_raw,
        "court_slug": submission.court_slug,
        "status": submission.status,
        "issues": [issue.model_dump(mode="json") for issue in submission.issues],
        "cleaned": _json_safe(submission.cleaned),
        "raw": _json_safe(submission.raw),
    }


def _has_blocking_issue(submission: CourtSubmission) -> bool:
    return any(issue.severity == "error" for issue in submission.issues)


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")

    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}

    if isinstance(value, list):
        return [_json_safe(item) for item in value]

    return value
