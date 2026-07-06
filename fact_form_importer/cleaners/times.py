"""Time parsing helpers."""

from __future__ import annotations

import re
from typing import Optional

from fact_form_importer.cleaners import CleaningResult
from fact_form_importer.cleaners.strings import null_if_empty_like
from fact_form_importer.models.issues import Issue

KNOWN_TEXT_STATUSES = {
    "appointment only",
    "appointments only",
    "by appointment only",
    "no counter service",
    "no counter service available",
    "closed",
}
TIME_PATTERN = re.compile(r"^(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?$", re.IGNORECASE)


def parse_time_parts(
    hour_value: object,
    minute_value: object,
    field: str = "time",
) -> CleaningResult:
    hour = null_if_empty_like(hour_value)
    minute = null_if_empty_like(minute_value)

    if hour is None and minute is None:
        return CleaningResult(value=None, status="empty")

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

    match = TIME_PATTERN.match(cleaned.replace(".", ""))
    if not match:
        return _invalid_time(field, value, "Time value could not be parsed")

    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = match.group(3)

    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0

    return _format_time(hour, minute, field, value)


def _parse_hour_minute(
    hour: str,
    minute: str,
    field: str,
    raw_value: object,
) -> CleaningResult:
    if not hour.isdigit() or not minute.isdigit():
        return _invalid_time(field, raw_value, "Hour and minute must be numeric")

    return _format_time(int(hour), int(minute), field, raw_value)


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
