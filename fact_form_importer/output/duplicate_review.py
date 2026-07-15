"""Shared duplicate-form grouping and date comparison helpers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from openpyxl.utils.datetime import from_excel

from fact_form_importer.models.court_submission import CourtSubmission


DATE_FIELDS = (
    ("completion_time", "Completion time"),
    ("last_modified_time", "Last modified time"),
    ("start_time", "Start time"),
)
SUBMISSION_SELECTION_VERSION = "1.0"
LATEST_SUBMISSION_POLICY_VERSION = "latest-completed-submission-v1"


@dataclass(frozen=True)
class DuplicateTimestamp:
    """The timestamp used to compare one form with its duplicate forms."""

    source_field: str
    source_label: str
    raw_value: str
    display_value: str
    sort_key: tuple[int, float, str]


def group_duplicate_submissions(
    submissions: Iterable[CourtSubmission],
) -> dict[str, list[CourtSubmission]]:
    """Group repeated cleaned court slugs independently of validation issues."""

    groups: dict[str, list[CourtSubmission]] = defaultdict(list)
    for submission in submissions:
        if submission.court_slug:
            groups[submission.court_slug or "(no cleaned court slug)"].append(submission)
    return {slug: rows for slug, rows in groups.items() if len(rows) > 1}


def duplicate_timestamp(submission: CourtSubmission) -> DuplicateTimestamp | None:
    """Use the first available Microsoft Forms timestamp for a date-only candidate."""

    for field, label in DATE_FIELDS:
        value = getattr(submission.source, field)
        if value:
            raw_value = str(value)
            return DuplicateTimestamp(
                source_field=field,
                source_label=label,
                raw_value=raw_value,
                display_value=format_source_date(raw_value),
                sort_key=_date_sort_key(raw_value),
            )
    return None


def most_recent_duplicate_submission(
    submissions: Iterable[CourtSubmission],
) -> CourtSubmission | None:
    """Return the latest submission, using the source row as a safe final fallback."""

    candidates = list(submissions)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda submission: (
            duplicate_timestamp(submission).sort_key
            if duplicate_timestamp(submission)
            else (0, 0.0, ""),
            submission.source.source_row_number,
        ),
    )


def select_authoritative_submissions(
    submissions: Iterable[CourtSubmission],
) -> tuple[list[CourtSubmission], dict[str, Any]]:
    """Apply the immutable latest-form policy and mark older duplicate rows skipped."""

    all_submissions = list(submissions)
    groups = group_duplicate_submissions(all_submissions)
    selected_by_slug = {
        slug: most_recent_duplicate_submission(rows) for slug, rows in groups.items()
    }
    evidence_groups: list[dict[str, Any]] = []
    superseded_rows: list[int] = []
    for slug, rows in sorted(groups.items()):
        selected = selected_by_slug[slug]
        if selected is None:
            continue
        selected_row = selected.source.source_row_number
        selected_timestamp = duplicate_timestamp(selected)
        superseded: list[dict[str, Any]] = []
        for submission in sort_duplicate_submissions(rows):
            row = submission.source.source_row_number
            timestamp = duplicate_timestamp(submission)
            if row == selected_row:
                submission.selection_status = "authoritative"
                submission.superseded_by_source_row_number = None
                continue
            submission.selection_status = "superseded"
            submission.superseded_by_source_row_number = selected_row
            submission.status = "skipped"
            superseded_rows.append(row)
            superseded.append(_selection_row_evidence(submission, timestamp))
        evidence_groups.append(
            {
                "court_slug": slug,
                "authoritative_source_row_number": selected_row,
                "authoritative_timestamp": _timestamp_evidence(selected_timestamp),
                "superseded": superseded,
            }
        )

    authoritative = [
        submission
        for submission in all_submissions
        if submission.selection_status == "authoritative"
    ]
    return authoritative, {
        "selection_version": SUBMISSION_SELECTION_VERSION,
        "policy_version": LATEST_SUBMISSION_POLICY_VERSION,
        "source_submission_count": len(all_submissions),
        "authoritative_submission_count": len(authoritative),
        "duplicate_court_count": len(evidence_groups),
        "duplicate_source_row_count": sum(
            1 + len(group["superseded"]) for group in evidence_groups
        ),
        "superseded_submission_count": len(superseded_rows),
        "authoritative_source_row_numbers": sorted(
            submission.source.source_row_number for submission in authoritative
        ),
        "superseded_source_row_numbers": sorted(superseded_rows),
        "groups": evidence_groups,
    }


def _selection_row_evidence(
    submission: CourtSubmission, timestamp: DuplicateTimestamp | None
) -> dict[str, Any]:
    return {
        "source_row_number": submission.source.source_row_number,
        "timestamp": _timestamp_evidence(timestamp),
    }


def _timestamp_evidence(timestamp: DuplicateTimestamp | None) -> dict[str, str] | None:
    if timestamp is None:
        return None
    return {
        "source_field": timestamp.source_field,
        "source_label": timestamp.source_label,
        "raw_value": timestamp.raw_value,
        "display_value": timestamp.display_value,
    }


def sort_duplicate_submissions(submissions: Iterable[CourtSubmission]) -> list[CourtSubmission]:
    """Present the date candidate first, then other rows in a stable order."""

    return sorted(
        submissions,
        key=lambda submission: (
            duplicate_timestamp(submission).sort_key
            if duplicate_timestamp(submission)
            else (0, 0.0, ""),
            submission.source.source_row_number,
        ),
        reverse=True,
    )


def format_source_date(value: str | None) -> str | None:
    """Display Excel serial timestamps readably while retaining textual dates unchanged."""

    if not value:
        return None
    try:
        converted = from_excel(float(value))
    except (TypeError, ValueError):
        return str(value)
    if isinstance(converted, datetime):
        return converted.isoformat(sep=" ", timespec="minutes")
    return str(value)


def _date_sort_key(value: str) -> tuple[int, float, str]:
    try:
        converted = from_excel(float(value))
        if isinstance(converted, datetime):
            return (2, _as_utc_timestamp(converted), "")
    except (TypeError, ValueError):
        pass

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (2, _as_utc_timestamp(parsed), "")
    except ValueError:
        return (1, 0.0, value.casefold())


def _as_utc_timestamp(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()
