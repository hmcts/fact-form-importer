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

    try:
        import phonenumbers

        parsed = phonenumbers.parse(candidate, "GB")
        if phonenumbers.is_valid_number(parsed):
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
                message="Phone number could not be parsed as a valid UK phone number",
                raw_value=value,
                cleaned_value=candidate,
            )
        ],
    )


def _looks_like_uk_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 10 <= len(digits) <= 11 and (digits.startswith("0") or digits.startswith("44"))


def _fallback_format_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("44") and len(digits) == 12:
        digits = "0" + digits[2:]

    if len(digits) == 11 and digits.startswith("020"):
        return f"{digits[:3]} {digits[3:7]} {digits[7:]}"

    if len(digits) == 11 and digits.startswith("0"):
        return f"{digits[:5]} {digits[5:]}"

    return re.sub(r"\s+", " ", value).strip()
