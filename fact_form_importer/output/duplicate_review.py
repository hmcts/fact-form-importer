"""Shared duplicate-form grouping and date comparison helpers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from openpyxl.utils.datetime import from_excel

from fact_form_importer.models.court_submission import CourtSubmission
from fact_form_importer.validators.base import DUPLICATE_COURT_SLUG


DATE_FIELDS = (
    ("completion_time", "Completion time"),
    ("last_modified_time", "Last modified time"),
    ("start_time", "Start time"),
)


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
    """Group only records explicitly flagged as duplicate court submissions."""

    groups: dict[str, list[CourtSubmission]] = defaultdict(list)
    for submission in submissions:
        if any(issue.code == DUPLICATE_COURT_SLUG for issue in submission.issues):
            groups[submission.court_slug or "(no cleaned court slug)"].append(submission)
    return dict(groups)


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
    """Return a date-based candidate only; it never makes an import decision."""

    dated = [submission for submission in submissions if duplicate_timestamp(submission)]
    if not dated:
        return None
    return max(
        dated,
        key=lambda submission: (
            duplicate_timestamp(submission).sort_key,  # type: ignore[union-attr]
            submission.source.source_row_number,
        ),
    )


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
