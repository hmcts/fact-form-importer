from fact_form_importer.models.court_import import CourtImportRecord
from fact_form_importer.models.court_submission import (
    Address,
    ContactDetail,
    CourtSubmission,
    OpeningHoursSet,
    OpeningTime,
)
from fact_form_importer.models.issues import Issue
from fact_form_importer.models.source import SourceMetadata


def test_court_submission_can_be_dumped_to_json_mode():
    submission = CourtSubmission(
        source=SourceMetadata(
            source_row_number=2,
            forms_id="123",
            submitter_email="submitter@example.test",
        ),
        court_slug_raw="https://example.test/courts/example-court",
        court_slug="example-court",
        facilities={"accessible_parking": True},
        translation_phone="020 7946 0000",
        addresses=[
            Address(
                index=1,
                address_type="Visit",
                line_1="1 Example Street",
                town_or_city="London",
                postcode="SW1A 1AA",
            )
        ],
        contacts=[
            ContactDetail(
                index=1,
                description="Enquiries",
                phone="020 7946 0000",
            )
        ],
        opening_hours=[
            OpeningHoursSet(
                index=1,
                type="Court open",
                same_monday_to_friday=True,
                monday_to_friday=OpeningTime(open="09:00", close="17:00"),
            )
        ],
        raw={"G": "https://example.test/courts/example-court"},
        cleaned={"court_slug": "example-court"},
        issues=[
            Issue(
                field="court_slug",
                code="normalised",
                severity="info",
                message="Slug was normalised from URL",
                raw_value="https://example.test/courts/example-court",
                cleaned_value="example-court",
            )
        ],
        status="processed_with_warnings",
    )

    dumped = submission.model_dump(mode="json")

    assert dumped["source"]["source_row_number"] == 2
    assert dumped["addresses"][0]["postcode"] == "SW1A 1AA"
    assert dumped["opening_hours"][0]["monday_to_friday"]["open"] == "09:00"
    assert dumped["issues"][0]["severity"] == "info"
    assert dumped["status"] == "processed_with_warnings"


def test_court_import_record_can_be_dumped_to_json_mode():
    record = CourtImportRecord(
        court_slug="example-court",
        source_row_numbers=[2, 5],
        addresses=[Address(index=1, postcode="SW1A 1AA")],
        contacts=[ContactDetail(index=1, email="court@example.test")],
        opening_hours=[OpeningHoursSet(index=1, type="Court open")],
        facilities={"wifi_available": True},
        issues=[],
        status="processed",
    )

    dumped = record.model_dump(mode="json")

    assert dumped["court_slug"] == "example-court"
    assert dumped["source_row_numbers"] == [2, 5]
    assert dumped["status"] == "processed"
