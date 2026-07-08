from fact_form_importer.models.court_submission import (
    Address,
    ContactDetail,
    CourtSubmission,
    OpeningHoursSet,
    OpeningTime,
)
from fact_form_importer.models.issues import Issue
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.validators.base import (
    COURT_SLUG_NORMALISED,
    COURT_SLUG_NOT_FOUND,
    DUPLICATE_COURT_SLUG,
    INVALID_EMAIL,
    INVALID_PHONE,
    INVALID_POSTCODE,
    MISSING_COURT_IDENTIFIER,
    OPENING_HOURS_AMBIGUOUS,
    VOCAB_NO_MATCH,
)
from fact_form_importer.validators.business_rules import (
    validate_all_submissions,
    validate_submission,
)
from fact_form_importer.validators.vocabularies import load_vocabularies


def test_validate_submission_marks_missing_slug_as_failed():
    submission = _submission(court_slug=None, court_slug_raw=None)

    validated = validate_submission(submission)

    assert validated.status == "failed"
    assert _has_issue(validated, MISSING_COURT_IDENTIFIER)


def test_validate_submission_marks_invalid_optional_email_and_phone_as_warnings():
    submission = _submission(
        translation_email="not-an-email",
        translation_phone="abc",
    )

    validated = validate_submission(submission)

    assert validated.status == "processed_with_warnings"
    assert _has_issue(validated, INVALID_EMAIL)
    assert _has_issue(validated, INVALID_PHONE)


def test_validate_submission_marks_slug_normalisation_as_warning_status():
    submission = _submission(
        court_slug_raw="https://www.find-court-tribunal.service.gov.uk/courts/example-court",
        court_slug="example-court",
    )

    validated = validate_submission(submission)

    assert validated.status == "processed_with_warnings"
    assert _has_issue(validated, COURT_SLUG_NORMALISED)


def test_validate_submission_marks_unknown_fact_api_slug_for_review():
    submission = _submission(court_slug="unknown-court")

    validated = validate_submission(submission, court_slug_exists=lambda slug: False)

    assert validated.status == "needs_human_review"
    assert _has_issue(validated, COURT_SLUG_NOT_FOUND)


def test_validate_submission_accepts_known_fact_api_slug():
    submission = _submission(court_slug="known-court")

    validated = validate_submission(submission, court_slug_exists=lambda slug: True)

    assert validated.status == "processed"
    assert not _has_issue(validated, COURT_SLUG_NOT_FOUND)


def test_validate_submission_marks_invalid_populated_postcode_for_review():
    submission = _submission(
        addresses=[
            Address(
                index=1,
                address_type="Visit",
                line_1="1 Example Street",
                town_or_city="London",
                postcode="BAD",
            )
        ]
    )

    validated = validate_submission(submission)

    assert validated.status == "needs_human_review"
    assert _has_issue(validated, INVALID_POSTCODE)


def test_validate_submission_marks_ambiguous_opening_hours_for_review():
    submission = _submission(
        opening_hours=[
            OpeningHoursSet(
                index=1,
                type="Court open",
                same_monday_to_friday=True,
                monday_to_friday=OpeningTime(status="invalid"),
            )
        ]
    )

    validated = validate_submission(submission)

    assert validated.status == "needs_human_review"
    assert _has_issue(validated, OPENING_HOURS_AMBIGUOUS)


def test_validate_submission_checks_vocabularies_when_available():
    vocabularies = load_vocabularies("config/vocabularies.example.json")
    submission = _submission(
        facilities={
            "hearing_enhancement_equipment": "Something else",
            "food_and_drink": ["Free water dispensers"],
        },
        addresses=[
            Address(
                index=1,
                address_type="Somewhere",
                postcode="SW1A 1AA",
                areas_of_law=["Civil"],
                court_types=["County Court"],
            )
        ],
        contacts=[ContactDetail(index=1, description="Enquiries", email="court@example.com")],
        opening_hours=[OpeningHoursSet(index=1, type="Court open")],
    )

    validated = validate_submission(submission, vocabularies)

    assert validated.status == "needs_human_review"
    assert [issue.code for issue in validated.issues].count(VOCAB_NO_MATCH) == 2


def test_validate_submission_passes_with_valid_values_and_vocabularies():
    vocabularies = load_vocabularies("config/vocabularies.example.json")
    submission = _submission(
        facilities={
            "hearing_enhancement_equipment": "Hearing loop systems are available at this court.",
            "food_and_drink": ["Free water dispensers"],
        },
        addresses=[
            Address(
                index=1,
                address_type="Visit",
                line_1="1 Example Street",
                town_or_city="London",
                postcode="SW1A 1AA",
                areas_of_law=["Civil"],
                court_types=["County Court"],
            )
        ],
        counter_service={
            "specific_courts": ["County Court"],
            "assists_with": ["Forms"],
            "appointment_contact": "020 7946 0000",
        },
        contacts=[ContactDetail(index=1, description="Enquiries", email="court@example.com")],
        opening_hours=[
            OpeningHoursSet(
                index=1,
                type="Court open",
                same_monday_to_friday=True,
                monday_to_friday=OpeningTime(open="09:00", close="17:00", status="valid_time"),
            )
        ],
    )

    validated = validate_submission(submission, vocabularies)

    assert validated.status == "processed"
    assert validated.issues == []


def test_validate_all_submissions_marks_duplicate_slugs_for_review():
    submissions = [
        _submission(row_number=2, court_slug="same-court"),
        _submission(row_number=3, court_slug="same-court"),
        _submission(row_number=4, court_slug="other-court"),
    ]

    validated = validate_all_submissions(submissions)

    assert validated[0].status == "needs_human_review"
    assert validated[1].status == "needs_human_review"
    assert validated[2].status == "processed"
    assert _has_issue(validated[0], DUPLICATE_COURT_SLUG)
    assert _has_issue(validated[1], DUPLICATE_COURT_SLUG)


def test_validate_all_submissions_caches_court_slug_lookups():
    calls = []
    submissions = [
        _submission(row_number=2, court_slug="same-court"),
        _submission(row_number=3, court_slug="same-court"),
        _submission(row_number=4, court_slug="other-court"),
    ]

    validate_all_submissions(
        submissions,
        court_slug_exists=lambda slug: calls.append(slug) or True,
    )

    assert calls == ["same-court", "other-court"]


def test_validate_submission_preserves_existing_cleaner_issues_without_duplicates():
    submission = _submission(
        issues=[
            Issue(
                field="translation_email",
                code=INVALID_EMAIL,
                severity="warning",
                message="Already invalid",
                raw_value="bad",
                cleaned_value="bad",
            )
        ],
        translation_email="bad",
    )

    validated = validate_submission(submission)

    assert validated.status == "processed_with_warnings"
    assert [issue.code for issue in validated.issues].count(INVALID_EMAIL) == 1


def _submission(
    row_number=2,
    court_slug="example-court",
    court_slug_raw=None,
    facilities=None,
    translation_phone=None,
    translation_email=None,
    addresses=None,
    counter_service=None,
    interview_rooms=None,
    contacts=None,
    opening_hours=None,
    issues=None,
):
    if court_slug_raw is None:
        court_slug_raw = court_slug

    return CourtSubmission(
        source=SourceMetadata(source_row_number=row_number),
        court_slug_raw=court_slug_raw,
        court_slug=court_slug,
        facilities=facilities or {},
        translation_phone=translation_phone,
        translation_email=translation_email,
        addresses=addresses or [],
        counter_service=counter_service or {},
        interview_rooms=interview_rooms or {},
        contacts=contacts or [],
        opening_hours=opening_hours or [],
        issues=issues or [],
    )


def _has_issue(submission, code):
    return any(issue.code == code for issue in submission.issues)
