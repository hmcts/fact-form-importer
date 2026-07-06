"""UK postcode cleaning helpers."""

from __future__ import annotations

import re

from fact_form_importer.cleaners import CleaningResult
from fact_form_importer.cleaners.strings import null_if_empty_like
from fact_form_importer.models.issues import Issue

POSTCODE_PATTERN = re.compile(
    r"^(GIR 0AA|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})$",
    re.IGNORECASE,
)


def normalise_uk_postcode(value: object, field: str = "postcode") -> CleaningResult:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return CleaningResult(value=None)

    candidate = re.sub(r"\s+", "", cleaned).upper()
    if candidate == "GIR0AA":
        return CleaningResult(value="GIR 0AA")

    if not POSTCODE_PATTERN.match(candidate):
        return CleaningResult(
            value=cleaned.upper(),
            issues=[
                Issue(
                    field=field,
                    code="INVALID_POSTCODE",
                    severity="warning",
                    message="Postcode does not match expected UK postcode format",
                    raw_value=value,
                    cleaned_value=cleaned.upper(),
                )
            ],
        )

    return CleaningResult(value=f"{candidate[:-3]} {candidate[-3:]}")
