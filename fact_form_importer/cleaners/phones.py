"""Phone number cleaning helpers."""

from __future__ import annotations

import re

from fact_form_importer.cleaners import CleaningResult
from fact_form_importer.cleaners.strings import null_if_empty_like
from fact_form_importer.models.issues import Issue


def normalise_uk_phone(value: object, field: str = "phone") -> CleaningResult:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return CleaningResult(value=None)

    candidate = cleaned
    extracted = extract_uk_phones(candidate)
    if extracted:
        return CleaningResult(value=extracted[0])

    try:
        import phonenumbers

        parsed = phonenumbers.parse(candidate, "GB")
        if phonenumbers.is_possible_number(parsed):
            return CleaningResult(
                value=phonenumbers.format_number(
                    parsed,
                    phonenumbers.PhoneNumberFormat.NATIONAL,
                )
            )
    except ModuleNotFoundError:
        if _looks_like_uk_phone(candidate):
            return CleaningResult(value=_fallback_format_phone(candidate))
    except Exception:
        pass

    return CleaningResult(
        value=candidate,
        issues=[
            Issue(
                field=field,
                code="INVALID_PHONE",
                severity="warning",
                message="Phone number could not be parsed as a possible UK phone number",
                raw_value=value,
                cleaned_value=candidate,
            )
        ],
    )


def extract_uk_phones(value: object) -> list[str]:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return []

    try:
        import phonenumbers

        numbers = []
        for match in phonenumbers.PhoneNumberMatcher(cleaned, "GB"):
            parsed = match.number
            if phonenumbers.is_possible_number(parsed):
                numbers.append(
                    phonenumbers.format_number(
                        parsed,
                        phonenumbers.PhoneNumberFormat.NATIONAL,
                    )
                )
        return numbers
    except ModuleNotFoundError:
        if _looks_like_uk_phone(cleaned):
            return [_fallback_format_phone(cleaned)]
        return []


def _looks_like_uk_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return (10 <= len(digits) <= 11 and digits.startswith("0")) or (
        len(digits) == 12 and digits.startswith("44")
    )


def _fallback_format_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("44") and len(digits) == 12:
        digits = "0" + digits[2:]

    if len(digits) == 11 and digits.startswith("020"):
        return f"{digits[:3]} {digits[3:7]} {digits[7:]}"

    if len(digits) == 11 and digits.startswith("0"):
        return f"{digits[:5]} {digits[5:]}"

    return re.sub(r"\s+", " ", value).strip()
