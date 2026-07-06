"""Email cleaning helpers."""

from __future__ import annotations

import re

from fact_form_importer.cleaners import CleaningResult
from fact_form_importer.cleaners.strings import null_if_empty_like
from fact_form_importer.models.issues import Issue

FALLBACK_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalise_email(value: object, field: str = "email") -> CleaningResult:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return CleaningResult(value=None)

    candidate = cleaned.lower()

    try:
        from email_validator import EmailNotValidError, validate_email

        try:
            validated = validate_email(candidate, check_deliverability=False)
            return CleaningResult(value=validated.normalized)
        except EmailNotValidError as exc:
            return CleaningResult(
                value=candidate,
                issues=[
                    Issue(
                        field=field,
                        code="INVALID_EMAIL",
                        severity="warning",
                        message=str(exc),
                        raw_value=value,
                        cleaned_value=candidate,
                    )
                ],
            )
    except ModuleNotFoundError:
        if FALLBACK_EMAIL_PATTERN.match(candidate):
            return CleaningResult(value=candidate)

        return CleaningResult(
            value=candidate,
            issues=[
                Issue(
                    field=field,
                    code="INVALID_EMAIL",
                    severity="warning",
                    message="Email address does not match a basic email pattern",
                    raw_value=value,
                    cleaned_value=candidate,
                )
            ],
        )
