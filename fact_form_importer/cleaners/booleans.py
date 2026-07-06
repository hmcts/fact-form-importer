"""Boolean cleaning helpers."""

from __future__ import annotations

from fact_form_importer.cleaners import CleaningResult
from fact_form_importer.cleaners.strings import null_if_empty_like
from fact_form_importer.models.issues import Issue

YES_VALUES = {"yes", "y", "true", "t", "1"}
NO_VALUES = {"no", "n", "false", "f", "0"}


def normalise_yes_no(value: object, field: str = "yes_no") -> CleaningResult:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return CleaningResult(value=None)

    lowered = cleaned.lower()
    if lowered in YES_VALUES:
        return CleaningResult(value=True)

    if lowered in NO_VALUES:
        return CleaningResult(value=False)

    return CleaningResult(
        value=None,
        issues=[
            Issue(
                field=field,
                code="UNKNOWN_BOOLEAN",
                severity="warning",
                message="Value could not be interpreted as yes/no",
                raw_value=value,
                cleaned_value=None,
            )
        ],
    )
