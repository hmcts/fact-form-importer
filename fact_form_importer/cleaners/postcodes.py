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
POSTCODE_TYPO_REPAIRED = "POSTCODE_TYPO_REPAIRED"


def normalise_uk_postcode(value: object, field: str = "postcode") -> CleaningResult:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return CleaningResult(value=None)

    candidate = re.sub(r"\s+", "", cleaned).upper()
    if candidate == "GIR0AA":
        return CleaningResult(value="GIR 0AA")

    if POSTCODE_PATTERN.match(candidate):
        return CleaningResult(value=f"{candidate[:-3]} {candidate[-3:]}")

    repaired_candidate = _repair_obvious_digit_slot_typo(candidate)
    if repaired_candidate is not None:
        return CleaningResult(
            value=f"{repaired_candidate[:-3]} {repaired_candidate[-3:]}",
            issues=[
                Issue(
                    field=field,
                    code=POSTCODE_TYPO_REPAIRED,
                    severity="warning",
                    message="Postcode had an obvious O/0 typo repaired in a digit position",
                    raw_value=value,
                    cleaned_value=f"{repaired_candidate[:-3]} {repaired_candidate[-3:]}",
                )
            ],
        )

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


def _repair_obvious_digit_slot_typo(candidate: str) -> str | None:
    if "O" not in candidate:
        return None

    for pattern in [
        r"^([A-Z]{1,2})O([A-Z\d]?\d[A-Z]{2})$",
        r"^([A-Z]{1,2}\d)O(\d[A-Z]{2})$",
    ]:
        repaired = re.sub(pattern, r"\g<1>0\g<2>", candidate)
        if repaired != candidate and POSTCODE_PATTERN.match(repaired):
            return repaired

    return None
