"""Time parsing helpers."""

from __future__ import annotations

import re

from fact_form_importer.cleaners import CleaningResult
from fact_form_importer.cleaners.strings import null_if_empty_like
from fact_form_importer.models.issues import Issue

KNOWN_TEXT_STATUSES = {
    "appointment only",
    "appointments only",
    "by appointment only",
    "no counter service",
    "no counter service available",
    "counter service not available",
    "closed",
}
TIME_PATTERN = re.compile(r"^(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?$", re.IGNORECASE)
_NOISY_FULL_TIME_PATTERN = re.compile(
    r"^(\d{1,2}:\d{2})\s*(am|pm)?\s*(?::\s*(?:\?|\d+\s*minutes?)|\?)$",
    re.IGNORECASE,
)
_MINUTE_NOISE_PATTERN = re.compile(r"^(?:\?|\d+\s*minutes?)$", re.IGNORECASE)


def parse_time_parts(
    hour_value: object,
    minute_value: object,
    field: str = "time",
) -> CleaningResult:
    hour = null_if_empty_like(hour_value)
    minute = null_if_empty_like(minute_value)
    hour = _normalise_time_punctuation(hour)
    minute = _normalise_time_punctuation(minute)

    if hour is None and minute is None:
        return CleaningResult(value=None, status="empty")

    if hour is not None and minute is not None and hour.lower() == minute.lower():
        if hour.lower() in KNOWN_TEXT_STATUSES:
            return CleaningResult(value=None, status="known_text_status")

    recovered = _recover_full_time_with_minute_noise(
        hour, minute, field, raw_value=f"{hour_value}:{minute_value}"
    )
    if recovered is not None:
        return recovered

    full_time_from_hour = _parse_full_time_from_hour_field(hour, minute, field, hour_value)
    if full_time_from_hour is not None:
        return full_time_from_hour

    if hour is None or minute is None:
        return _invalid_time(field, f"{hour_value}:{minute_value}", "Both hour and minute are required")

    return _parse_hour_minute(hour, minute, field, raw_value=f"{hour_value}:{minute_value}")


def parse_time_cell(value: object, field: str = "time") -> CleaningResult:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return CleaningResult(value=None, status="empty")

    lowered = cleaned.lower()
    if lowered in KNOWN_TEXT_STATUSES:
        return CleaningResult(value=None, status="known_text_status")

    cleaned = _normalise_time_punctuation(cleaned) or ""

    noisy_match = _NOISY_FULL_TIME_PATTERN.fullmatch(cleaned)
    if noisy_match:
        parsed = _parse_clock_value(
            noisy_match.group(1), noisy_match.group(2), field, value
        )
        if parsed.status == "valid_time" and parsed.value:
            return _recovered_time(field, value, parsed.value)

    match = TIME_PATTERN.match(cleaned.replace(".", ":"))
    if not match:
        return _invalid_time(field, value, "Time value could not be parsed")

    return _parse_clock_value(
        f"{match.group(1)}:{match.group(2) or '0'}", match.group(3), field, value
    )


def _parse_clock_value(
    clock: str, meridiem: str | None, field: str, raw_value: object
) -> CleaningResult:
    hour_text, minute_text = clock.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if meridiem and hour <= 12:
        meridiem = meridiem.lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    return _format_time(hour, minute, field, raw_value)


def _recover_full_time_with_minute_noise(
    hour: str | None,
    minute: str | None,
    field: str,
    raw_value: object,
) -> CleaningResult | None:
    """Recover a complete time when the separate minute cell is obvious noise."""

    if hour is None or minute is None or not _MINUTE_NOISE_PATTERN.fullmatch(minute):
        return None
    parsed = parse_time_cell(hour, field)
    if parsed.status != "valid_time" or parsed.value is None:
        return None
    return _recovered_time(field, raw_value, parsed.value)


def _recovered_time(field: str, raw_value: object, value: str) -> CleaningResult:
    return CleaningResult(
        value=value,
        status="valid_time",
        issues=[
            Issue(
                field=field,
                code="TIME_FORMAT_RECOVERED",
                severity="warning",
                message="Recovered one unambiguous time and ignored recognised formatting noise",
                raw_value=raw_value,
                cleaned_value=value,
            )
        ],
    )


def _parse_hour_minute(
    hour: str,
    minute: str,
    field: str,
    raw_value: object,
) -> CleaningResult:
    if not hour.isdigit() or not minute.isdigit():
        return _invalid_time(field, raw_value, "Hour and minute must be numeric")

    return _format_time(int(hour), int(minute), field, raw_value)


def _parse_full_time_from_hour_field(
    hour: str | None,
    minute: str | None,
    field: str,
    raw_value: object,
) -> CleaningResult | None:
    if hour is None or not _looks_like_full_time(hour):
        return None

    hour_result = parse_time_cell(hour, field)
    if hour_result.status != "valid_time" or hour_result.value is None:
        return None

    expected_minute = hour_result.value.split(":", 1)[1]
    if minute is None:
        return CleaningResult(value=hour_result.value, status="valid_time")

    if _looks_like_full_time(minute):
        minute_result = parse_time_cell(minute, field)
        if minute_result.status == "valid_time" and minute_result.value == hour_result.value:
            return CleaningResult(value=hour_result.value, status="valid_time")
        if (
            minute_result.status == "valid_time"
            and minute_result.value == "00:00"
            and hour_result.value.endswith(":00")
        ):
            return CleaningResult(value=hour_result.value, status="valid_time")
        return None

    if minute.isdigit() and f"{int(minute):02d}" == expected_minute:
        return CleaningResult(value=hour_result.value, status="valid_time")

    return None


def _looks_like_full_time(value: str) -> bool:
    return bool(re.match(r"^\d{1,2}[:.]\d{2}\s*(am|pm)?$", value, re.IGNORECASE))


def _normalise_time_punctuation(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.replace(";", ":")
    return value.strip(".:")


def _format_time(hour: int, minute: int, field: str, raw_value: object) -> CleaningResult:
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return CleaningResult(value=f"{hour:02d}:{minute:02d}", status="valid_time")

    return _invalid_time(field, raw_value, "Time is outside the valid 00:00-23:59 range")


def _invalid_time(field: str, raw_value: object, message: str) -> CleaningResult:
    return CleaningResult(
        value=None,
        status="invalid",
        issues=[
            Issue(
                field=field,
                code="INVALID_TIME",
                severity="warning",
                message=message,
                raw_value=raw_value,
                cleaned_value=None,
            )
        ],
    )
