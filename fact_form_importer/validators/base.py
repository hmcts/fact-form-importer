"""Submission validation and status calculation."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional

from fact_form_importer.cleaners.emails import normalise_email
from fact_form_importer.cleaners.phones import normalise_uk_phone
from fact_form_importer.cleaners.postcodes import normalise_uk_postcode
from fact_form_importer.cleaners.strings import null_if_empty_like
from fact_form_importer.models.court_submission import CourtSubmission, OpeningTime
from fact_form_importer.models.issues import Issue
from fact_form_importer.validators.fact_api_courts import CourtSlugSuggestion
from fact_form_importer.validators.vocabularies import Vocabularies

MISSING_COURT_IDENTIFIER = "MISSING_COURT_IDENTIFIER"
COURT_SLUG_NORMALISED = "COURT_SLUG_NORMALISED"
COURT_SLUG_NOT_FOUND = "COURT_SLUG_NOT_FOUND"
COURT_SLUG_SUGGESTED = "COURT_SLUG_SUGGESTED"
COURT_SLUG_AUTO_REPAIRED = "COURT_SLUG_AUTO_REPAIRED"
DUPLICATE_COURT_SLUG = "DUPLICATE_COURT_SLUG"
INVALID_EMAIL = "INVALID_EMAIL"
INVALID_PHONE = "INVALID_PHONE"
INVALID_POSTCODE = "INVALID_POSTCODE"
INVALID_TIME = "INVALID_TIME"
VOCAB_NO_MATCH = "VOCAB_NO_MATCH"
OPENING_HOURS_AMBIGUOUS = "OPENING_HOURS_AMBIGUOUS"
LLM_NORMALISATION_FAILED = "LLM_NORMALISATION_FAILED"
LLM_LOW_CONFIDENCE = "LLM_LOW_CONFIDENCE"
LLM_REVIEW_REQUIRED = "LLM_REVIEW_REQUIRED"
LLM_RETURNED_INVALID_VOCAB_VALUE = "LLM_RETURNED_INVALID_VOCAB_VALUE"
LLM_RETURNED_INVALID_VALUE = "LLM_RETURNED_INVALID_VALUE"
LLM_RETURNED_UNEXPECTED_FIELD = "LLM_RETURNED_UNEXPECTED_FIELD"
LLM_RETURNED_SENSITIVE_VALUE = "LLM_RETURNED_SENSITIVE_VALUE"

FAILED_ISSUE_CODES = {
    MISSING_COURT_IDENTIFIER,
}
LLM_HUMAN_REVIEW_ISSUE_CODES = {
    LLM_NORMALISATION_FAILED,
    LLM_LOW_CONFIDENCE,
    LLM_REVIEW_REQUIRED,
    LLM_RETURNED_INVALID_VOCAB_VALUE,
    LLM_RETURNED_INVALID_VALUE,
    LLM_RETURNED_UNEXPECTED_FIELD,
    LLM_RETURNED_SENSITIVE_VALUE,
}
HUMAN_REVIEW_ISSUE_CODES = {
    COURT_SLUG_NOT_FOUND,
    DUPLICATE_COURT_SLUG,
    INVALID_POSTCODE,
    INVALID_TIME,
    VOCAB_NO_MATCH,
    OPENING_HOURS_AMBIGUOUS,
    *LLM_HUMAN_REVIEW_ISSUE_CODES,
}
WARNING_STATUS_ISSUE_CODES = {
    COURT_SLUG_NORMALISED,
    COURT_SLUG_AUTO_REPAIRED,
    COURT_SLUG_SUGGESTED,
    INVALID_EMAIL,
    INVALID_PHONE,
}
VALIDATION_ISSUE_CODES = {
    MISSING_COURT_IDENTIFIER,
    COURT_SLUG_NORMALISED,
    COURT_SLUG_NOT_FOUND,
    COURT_SLUG_SUGGESTED,
    COURT_SLUG_AUTO_REPAIRED,
    DUPLICATE_COURT_SLUG,
    INVALID_EMAIL,
    INVALID_PHONE,
    INVALID_POSTCODE,
    INVALID_TIME,
    VOCAB_NO_MATCH,
    OPENING_HOURS_AMBIGUOUS,
}
COURT_SLUG_AUTO_REPAIR_CONFIDENCE_THRESHOLD = 0.95
TIME_PATTERN = re.compile(r"^\d{2}:\d{2}$")


def validate_submission(
    submission: CourtSubmission,
    vocabularies: Optional[Vocabularies] = None,
    court_slug_exists: Optional[Callable[[str], bool]] = None,
    court_slug_suggester: Optional[Callable[[str, str | None], CourtSlugSuggestion | None]] = None,
) -> CourtSubmission:
    """Validate one already-ingested submission and recalculate its status."""

    _validate_required_court_slug(submission)
    _validate_court_slug_exists(submission, court_slug_exists, court_slug_suggester)
    _validate_slug_normalisation(submission)
    _validate_facility_fields(submission, vocabularies)
    _validate_translation_fields(submission)
    _validate_addresses(submission, vocabularies)
    _validate_counter_service(submission, vocabularies)
    _validate_interview_rooms(submission)
    _validate_contacts(submission, vocabularies)
    _validate_opening_hours(submission, vocabularies)
    submission.status = calculate_status(submission)
    return submission


def validate_all_submissions(
    submissions: list[CourtSubmission],
    vocabularies: Optional[Vocabularies] = None,
    court_slug_exists: Optional[Callable[[str], bool]] = None,
    court_slug_suggester: Optional[Callable[[str, str | None], CourtSlugSuggestion | None]] = None,
) -> list[CourtSubmission]:
    """Validate submissions and flag duplicate court slugs across the batch."""

    cached_court_slug_exists = _cache_court_slug_exists(court_slug_exists)
    cached_court_slug_suggester = _cache_court_slug_suggester(court_slug_suggester)
    validated = [
        validate_submission(
            submission,
            vocabularies,
            cached_court_slug_exists,
            cached_court_slug_suggester,
        )
        for submission in submissions
    ]
    from fact_form_importer.validators.duplicates import flag_duplicate_court_slugs

    flag_duplicate_court_slugs(validated)
    for submission in validated:
        submission.status = calculate_status(submission)
    return validated


def calculate_status(submission: CourtSubmission) -> str:
    issue_codes = {issue.code for issue in submission.issues}

    if any(issue.severity == "error" for issue in submission.issues):
        return "failed"

    if issue_codes & FAILED_ISSUE_CODES:
        return "failed"

    if issue_codes & HUMAN_REVIEW_ISSUE_CODES:
        return "needs_human_review"

    if any(issue.severity == "warning" for issue in submission.issues):
        return "processed_with_warnings"

    if issue_codes & WARNING_STATUS_ISSUE_CODES:
        return "processed_with_warnings"

    return "processed"


def add_issue_once(submission: CourtSubmission, issue: Issue) -> None:
    if not any(
        existing.field == issue.field
        and existing.code == issue.code
        and existing.raw_value == issue.raw_value
        for existing in submission.issues
    ):
        submission.issues.append(issue)


def clear_validation_issues(submissions: Iterable[CourtSubmission]) -> None:
    """Remove derived validation issues before validating changed submissions again.

    Ingest/cleaner and LLM audit issues are deliberately retained. Validation
    issues are rebuilt from the current cleaned model values.
    """

    for submission in submissions:
        submission.issues = [
            issue for issue in submission.issues if issue.code not in VALIDATION_ISSUE_CODES
        ]


def _validate_required_court_slug(submission: CourtSubmission) -> None:
    if null_if_empty_like(submission.court_slug) is not None:
        return

    add_issue_once(
        submission,
        Issue(
            field="court_slug",
            code=MISSING_COURT_IDENTIFIER,
            severity="error",
            message="Court identifier is required",
            raw_value=submission.court_slug_raw,
            cleaned_value=submission.court_slug,
        ),
    )


def _validate_slug_normalisation(submission: CourtSubmission) -> None:
    raw = null_if_empty_like(submission.court_slug_raw)
    if raw is None or submission.court_slug is None:
        return

    if raw == submission.court_slug:
        return

    add_issue_once(
        submission,
        Issue(
            field="court_slug",
            code=COURT_SLUG_NORMALISED,
            severity="info",
            message="Court slug was normalised from the submitted value",
            raw_value=submission.court_slug_raw,
            cleaned_value=submission.court_slug,
        ),
    )


def _validate_court_slug_exists(
    submission: CourtSubmission,
    court_slug_exists: Optional[Callable[[str], bool]],
    court_slug_suggester: Optional[Callable[[str, str | None], CourtSlugSuggestion | None]],
) -> None:
    if court_slug_exists is None or null_if_empty_like(submission.court_slug) is None:
        return

    if court_slug_exists(str(submission.court_slug)):
        return

    suggestion = None
    if court_slug_suggester is not None:
        suggestion = court_slug_suggester(str(submission.court_slug), submission.court_slug_raw)
        if (
            suggestion is not None
            and suggestion.confidence >= COURT_SLUG_AUTO_REPAIR_CONFIDENCE_THRESHOLD
            and court_slug_exists(suggestion.suggested_slug)
        ):
            submitted_slug = submission.court_slug
            submission.court_slug = suggestion.suggested_slug
            add_issue_once(
                submission,
                Issue(
                    field="court_slug",
                    code=COURT_SLUG_AUTO_REPAIRED,
                    severity="warning",
                    message="Court slug was auto-repaired using a high-confidence FaCT search match",
                    raw_value=submission.court_slug_raw,
                    cleaned_value={
                        **suggestion.as_dict(),
                        "previous_cleaned_slug": submitted_slug,
                    },
                ),
            )
            return

        if suggestion is not None:
            add_issue_once(
                submission,
                Issue(
                    field="court_slug",
                    code=COURT_SLUG_SUGGESTED,
                    severity="warning",
                    message="FaCT search found a possible court slug suggestion for review",
                    raw_value=submission.court_slug_raw,
                    cleaned_value=suggestion.as_dict(),
                ),
            )

    add_issue_once(
        submission,
        Issue(
            field="court_slug",
            code=COURT_SLUG_NOT_FOUND,
            severity="warning",
            message="Court slug does not exist in FaCT Data API",
            raw_value=submission.court_slug_raw,
            cleaned_value=suggestion.as_dict() if suggestion is not None else submission.court_slug,
        ),
    )


def _cache_court_slug_exists(
    court_slug_exists: Optional[Callable[[str], bool]],
) -> Optional[Callable[[str], bool]]:
    if court_slug_exists is None:
        return None

    cache: dict[str, bool] = {}

    def cached(slug: str) -> bool:
        if slug not in cache:
            cache[slug] = court_slug_exists(slug)
        return cache[slug]

    return cached


def _cache_court_slug_suggester(
    court_slug_suggester: Optional[Callable[[str, str | None], CourtSlugSuggestion | None]],
) -> Optional[Callable[[str, str | None], CourtSlugSuggestion | None]]:
    if court_slug_suggester is None:
        return None

    cache: dict[tuple[str, str | None], CourtSlugSuggestion | None] = {}

    def cached(slug: str, raw_value: str | None) -> CourtSlugSuggestion | None:
        key = (slug, raw_value)
        if key not in cache:
            cache[key] = court_slug_suggester(slug, raw_value)
        return cache[key]

    return cached


def _validate_facility_fields(
    submission: CourtSubmission,
    vocabularies: Optional[Vocabularies],
) -> None:
    _validate_optional_phone(
        submission,
        "facilities.accessible_parking_phone",
        submission.facilities.get("accessible_parking_phone"),
    )
    _validate_optional_phone(
        submission,
        "facilities.accessible_entrance_support_phone",
        submission.facilities.get("accessible_entrance_support_phone"),
    )
    _validate_vocab_value(
        submission,
        "facilities.hearing_enhancement_equipment",
        submission.facilities.get("hearing_enhancement_equipment"),
        "hearing_enhancement_options",
        vocabularies,
    )
    _validate_vocab_values(
        submission,
        "facilities.food_and_drink",
        submission.facilities.get("food_and_drink"),
        "food_and_drink_options",
        vocabularies,
    )


def _validate_translation_fields(submission: CourtSubmission) -> None:
    _validate_optional_phone(submission, "translation_phone", submission.translation_phone)
    _validate_optional_email(submission, "translation_email", submission.translation_email)


def _validate_addresses(
    submission: CourtSubmission,
    vocabularies: Optional[Vocabularies],
) -> None:
    for address in submission.addresses:
        prefix = f"addresses[{address.index}]"
        _validate_vocab_value(
            submission,
            f"{prefix}.address_type",
            address.address_type,
            "address_types",
            vocabularies,
        )
        _validate_optional_postcode(submission, f"{prefix}.postcode", address.postcode)
        _validate_vocab_values(
            submission,
            f"{prefix}.areas_of_law",
            address.areas_of_law,
            "areas_of_law",
            vocabularies,
        )
        _validate_vocab_values(
            submission,
            f"{prefix}.court_types",
            address.court_types,
            "court_types",
            vocabularies,
        )


def _validate_counter_service(
    submission: CourtSubmission,
    vocabularies: Optional[Vocabularies],
) -> None:
    _validate_vocab_values(
        submission,
        "counter_service.specific_courts",
        submission.counter_service.get("specific_courts"),
        "court_types",
        vocabularies,
    )
    _validate_vocab_values(
        submission,
        "counter_service.assists_with",
        submission.counter_service.get("assists_with"),
        "counter_service_assistance",
        vocabularies,
    )
    _validate_optional_phone_or_email(
        submission,
        "counter_service.appointment_contact",
        submission.counter_service.get("appointment_contact"),
    )
    for field_name in ["monday_to_friday", "monday", "tuesday", "wednesday", "thursday", "friday"]:
        _validate_opening_time(
            submission,
            f"counter_service.{field_name}",
            submission.counter_service.get(field_name),
        )


def _validate_interview_rooms(submission: CourtSubmission) -> None:
    _validate_optional_phone(
        submission,
        "interview_rooms.booking_phone",
        submission.interview_rooms.get("booking_phone"),
    )


def _validate_contacts(
    submission: CourtSubmission,
    vocabularies: Optional[Vocabularies],
) -> None:
    for contact in submission.contacts:
        prefix = f"contacts[{contact.index}]"
        _validate_vocab_value(
            submission,
            f"{prefix}.description",
            contact.description,
            "contact_description_types",
            vocabularies,
        )
        _validate_optional_phone(submission, f"{prefix}.phone", contact.phone)
        _validate_optional_email(submission, f"{prefix}.email", contact.email)


def _validate_opening_hours(
    submission: CourtSubmission,
    vocabularies: Optional[Vocabularies],
) -> None:
    for opening_hours in submission.opening_hours:
        prefix = f"opening_hours[{opening_hours.index}]"
        _validate_vocab_value(
            submission,
            f"{prefix}.type",
            opening_hours.type,
            "opening_hour_types",
            vocabularies,
        )
        for field_name in ["monday_to_friday", "monday", "tuesday", "wednesday", "thursday", "friday"]:
            _validate_opening_time(
                submission,
                f"{prefix}.{field_name}",
                getattr(opening_hours, field_name),
            )


def _validate_optional_email(submission: CourtSubmission, field: str, value: Any) -> None:
    if null_if_empty_like(value) is None:
        return

    for issue in normalise_email(value, field).issues:
        add_issue_once(submission, issue)


def _validate_optional_phone(submission: CourtSubmission, field: str, value: Any) -> None:
    if null_if_empty_like(value) is None:
        return

    for issue in normalise_uk_phone(value, field).issues:
        add_issue_once(submission, issue)


def _validate_optional_phone_or_email(
    submission: CourtSubmission,
    field: str,
    value: Any,
) -> None:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return

    email_result = normalise_email(cleaned, field)
    phone_result = normalise_uk_phone(cleaned, field)
    if not email_result.issues or not phone_result.issues:
        return

    add_issue_once(
        submission,
        Issue(
            field=field,
            code=INVALID_EMAIL,
            severity="warning",
            message="Value is not a valid email address or UK phone number",
            raw_value=value,
            cleaned_value=cleaned,
        ),
    )


def _validate_optional_postcode(submission: CourtSubmission, field: str, value: Any) -> None:
    if null_if_empty_like(value) is None:
        return

    for issue in normalise_uk_postcode(value, field).issues:
        add_issue_once(submission, issue)


def _validate_opening_time(
    submission: CourtSubmission,
    field: str,
    value: Optional[OpeningTime],
) -> None:
    if value is None:
        return

    for issue in value.issues:
        add_issue_once(submission, issue)

    if value.status in {"invalid", "known_text_status"}:
        add_issue_once(
            submission,
            Issue(
                field=field,
                code=OPENING_HOURS_AMBIGUOUS,
                severity="warning",
                message="Opening hours need human review",
                raw_value=value.model_dump(mode="json"),
                cleaned_value=None,
            ),
        )
        return

    for time_field, time_value in {"open": value.open, "close": value.close}.items():
        if time_value is None:
            continue
        if not TIME_PATTERN.match(time_value):
            add_issue_once(
                submission,
                Issue(
                    field=f"{field}.{time_field}",
                    code=INVALID_TIME,
                    severity="warning",
                    message="Time must be in HH:MM format",
                    raw_value=time_value,
                    cleaned_value=None,
                ),
            )


def _validate_vocab_value(
    submission: CourtSubmission,
    field: str,
    value: Any,
    vocabulary_name: str,
    vocabularies: Optional[Vocabularies],
) -> None:
    if vocabularies is None or null_if_empty_like(value) is None:
        return

    if vocabularies.normalised_vocab_match(value, vocabulary_name) is not None:
        return

    add_issue_once(
        submission,
        Issue(
            field=field,
            code=VOCAB_NO_MATCH,
            severity="warning",
            message=f"Value does not match vocabulary '{vocabulary_name}'",
            raw_value=value,
            cleaned_value=None,
        ),
    )


def _validate_vocab_values(
    submission: CourtSubmission,
    field: str,
    values: Any,
    vocabulary_name: str,
    vocabularies: Optional[Vocabularies],
) -> None:
    if values is None:
        return

    if isinstance(values, str):
        values_to_check: Iterable[Any] = [values]
    else:
        values_to_check = values

    for value in values_to_check:
        _validate_vocab_value(submission, field, value, vocabulary_name, vocabularies)
