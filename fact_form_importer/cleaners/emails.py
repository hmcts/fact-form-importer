"""Email cleaning helpers."""

from __future__ import annotations

import re

from fact_form_importer.cleaners import CleaningResult
from fact_form_importer.cleaners.strings import null_if_empty_like
from fact_form_importer.models.issues import Issue

FALLBACK_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_SEARCH_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")


def normalise_email(value: object, field: str = "email") -> CleaningResult:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return CleaningResult(value=None)

    candidate = _remove_invisible_characters(cleaned).lower()

    try:
        from email_validator import EmailNotValidError, validate_email

        try:
            validated = validate_email(candidate, check_deliverability=False)
            return CleaningResult(value=validated.normalized)
        except EmailNotValidError as exc:
            extracted = extract_email_addresses(candidate)
            if extracted:
                return CleaningResult(value=extracted[0])

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

        extracted = extract_email_addresses(candidate)
        if extracted:
            return CleaningResult(value=extracted[0])

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


def extract_email_addresses(value: object) -> list[str]:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return []

    candidate = _remove_invisible_characters(cleaned).lower()
    return [match.group(0).rstrip(".,;:") for match in EMAIL_SEARCH_PATTERN.finditer(candidate)]


def _remove_invisible_characters(value: str) -> str:
    return value.replace("\u200b", "").replace("\ufeff", "")
