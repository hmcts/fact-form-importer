"""Build the versioned JSON document for the future FaCT import controller."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pydantic import BaseModel

from fact_form_importer.models.court_submission import CourtSubmission
from fact_form_importer.output.fact_api_manifest import (
    _accessibility_options_body,
    _address_body,
    _building_facilities_body,
    _contact_body,
    _counter_service_body,
    _opening_hours_body,
    _professional_information_body,
    _translation_body,
)
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.validators.vocabularies import Vocabularies

IMPORTABLE_STATUSES = {"processed", "processed_with_warnings"}
IMPORT_PAYLOAD_SCHEMA_VERSION = "1.0"

CourtLookup = Callable[[str], Optional[CourtReference]]


def build_fact_import_payload(
    submissions: list[CourtSubmission],
    *,
    run_id: str,
    vocabularies: Vocabularies | None = None,
    court_lookup: CourtLookup | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the single request body expected by a future FaCT import controller.

    This is deliberately a data contract rather than a list of HTTP actions.
    It contains only records that passed the import status gate and excludes
    raw spreadsheet values, submitter metadata, and validation issues.
    """

    return {
        "schemaVersion": IMPORT_PAYLOAD_SCHEMA_VERSION,
        "runId": run_id,
        "generatedAt": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "records": [
            _import_record(submission, vocabularies=vocabularies, court_lookup=court_lookup)
            for submission in submissions
            if _is_importable(submission)
        ],
    }


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


def _import_record(
    submission: CourtSubmission,
    *,
    vocabularies: Vocabularies | None,
    court_lookup: CourtLookup | None,
) -> dict[str, Any]:
    court_reference = court_lookup(submission.court_slug) if court_lookup and submission.court_slug else None
    professional_information = _professional_information_body(submission).get(
        "professionalInformation", {}
    )
    counter_service, _ = _counter_service_body(submission, vocabularies)

    return {
        "courtId": court_reference.court_id if court_reference else None,
        "courtSlug": submission.court_slug,
        "sourceRowNumbers": [submission.source.source_row_number],
        "buildingFacilities": _building_facilities_body(submission.facilities),
        "accessibilityOptions": _accessibility_options_body(submission.facilities),
        "translationServices": _translation_body(submission),
        "professionalInformation": professional_information,
        "counterServiceOpeningHours": counter_service,
        "addresses": [
            body
            for address in submission.addresses
            if (body := _address_body(address, vocabularies)[0])
        ],
        "contactDetails": [
            body
            for contact in submission.contacts
            if (body := _contact_body(contact, vocabularies)[0])
        ],
        "openingHours": [
            body
            for opening_hours in submission.opening_hours
            if (body := _opening_hours_body(opening_hours, vocabularies)[0])
        ],
    }


def _is_importable(submission: CourtSubmission) -> bool:
    return submission.status in IMPORTABLE_STATUSES and not any(
        issue.severity == "error" for issue in submission.issues
    )


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


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value
