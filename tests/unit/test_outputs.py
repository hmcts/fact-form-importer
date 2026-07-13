import json

from fact_form_importer.ingest.workbook_profiler import WorkbookProfile
from fact_form_importer.ingest.workbook_reader import IngestResult
from fact_form_importer.models.court_submission import Address, ContactDetail, CourtSubmission, OpeningHoursSet, OpeningTime
from fact_form_importer.models.issues import Issue
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.output.fact_json import (
    build_fact_import_payload,
    build_failed_records,
    build_human_review_records,
)
from fact_form_importer.output.logs import build_import_summary, write_processing_outputs
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.validators.vocabularies import Vocabularies


def test_build_fact_import_payload_excludes_failed_and_human_review_records():
    submissions = [
        _submission("processed-court", "processed"),
        _submission("warning-court", "processed_with_warnings"),
        _submission("review-court", "needs_human_review"),
        _submission("failed-court", "failed"),
    ]

    payload = build_fact_import_payload(submissions, run_id="run-1")

    assert payload["schemaVersion"] == "1.0"
    assert payload["runId"] == "run-1"
    assert [record["courtSlug"] for record in payload["records"]] == [
        "processed-court",
        "warning-court",
    ]


def test_build_fact_import_payload_uses_camel_case_controller_sections():
    submission = _submission("processed-court", "processed")
    submission.facilities = {"parking_available": True, "accessible_parking": False}
    submission.counter_service = {
        "assists_with": ["Forms"],
        "monday_to_friday": OpeningTime(open="09:00", close="17:00", status="valid_time")
    }

    payload = build_fact_import_payload([submission], run_id="run-1")
    record = payload["records"][0]

    assert record["courtSlug"] == "processed-court"
    assert record["sourceRowNumbers"] == [2]
    assert record["buildingFacilities"] == {"parking": True}
    assert record["accessibilityOptions"] == {"accessibleParking": False}
    assert record["counterServiceOpeningHours"]["counterService"] is True
    assert set(record) == {
        "courtId",
        "courtSlug",
        "sourceRowNumbers",
        "buildingFacilities",
        "accessibilityOptions",
        "translationServices",
        "professionalInformation",
        "counterServiceOpeningHours",
        "addresses",
        "contactDetails",
        "openingHours",
    }


def test_build_fact_import_payload_preserves_api_ready_child_sections_without_review_data():
    submission = _submission("processed-court", "processed_with_warnings")
    submission.facilities = {
        "food_and_drink": ["Free water dispensers"],
        "quiet_room_available": True,
    }
    submission.translation_email = "translation@example.test"
    submission.interview_rooms = {"has_interview_rooms": True, "room_count": "2"}
    submission.addresses = [
        Address(
            index=1,
            address_type="Visit",
            line_1="1 Main Street",
            postcode="SW1A 1AA",
            areas_of_law=["Civil"],
            court_types=["County Court"],
        )
    ]
    submission.contacts = [
        ContactDetail(index=1, description="Enquiries", email="contact@example.test")
    ]
    submission.opening_hours = [
        OpeningHoursSet(
            index=1,
            type="Court open",
            same_monday_to_friday=True,
            monday_to_friday=OpeningTime(open="09:00", close="17:00", status="valid_time"),
        )
    ]
    submission.raw = {"submitter_email": "person@example.test"}
    submission.cleaned = {"internal": "not for import"}
    submission.issues = [
        Issue(field="court_slug", code="COURT_SLUG_NORMALISED", severity="warning", message="Normalised")
    ]
    vocabularies = Vocabularies(
        version="test",
        vocabularies={
            "areas_of_law": [{"code": "civil", "name": "Civil", "api_id": "area-id"}],
            "court_types": [{"code": "county", "name": "County Court", "api_id": "type-id"}],
            "contact_description_types": [{"code": "enquiries", "name": "Enquiries", "api_id": "contact-id"}],
            "opening_hour_types": [{"code": "court_open", "name": "Court open", "api_id": "opening-id"}],
        },
    )

    payload = build_fact_import_payload(
        [submission],
        run_id="run-1",
        vocabularies=vocabularies,
        court_lookup=lambda slug: CourtReference("court-id", slug),
    )
    record = payload["records"][0]

    assert record["courtId"] == "court-id"
    assert record["buildingFacilities"]["freeWaterDispensers"] is True
    assert record["accessibilityOptions"]["quietRoom"] is True
    assert record["translationServices"] == {"email": "translation@example.test"}
    assert record["professionalInformation"] == {"interviewRooms": True, "interviewRoomCount": 2}
    assert record["addresses"][0]["areasOfLaw"] == ["area-id"]
    assert record["addresses"][0]["courtTypes"] == ["type-id"]
    assert record["contactDetails"][0]["courtContactDescriptionId"] == "contact-id"
    assert record["openingHours"][0]["openingHourTypeId"] == "opening-id"
    assert record["openingHours"][0]["openingTimesDetails"][0]["dayOfWeek"] == "EVERYDAY"
    assert "raw" not in record and "issues" not in record and "source" not in record


def test_review_output_builders_include_only_matching_statuses():
    failed = _submission("failed-court", "failed")
    review = _submission("review-court", "needs_human_review")
    processed = _submission("processed-court", "processed")

    assert [record["court_slug"] for record in build_failed_records([failed, review, processed])] == [
        "failed-court"
    ]
    assert [
        record["court_slug"]
        for record in build_human_review_records([failed, review, processed])
    ] == ["review-court"]


def test_review_output_builders_serialise_nested_cleaned_models():
    review = _submission("review-court", "needs_human_review")
    review.cleaned = {
        "counter_service": {
            "monday_to_friday": OpeningTime(open="09:00", close="17:00", status="valid_time")
        }
    }

    record = build_human_review_records([review])[0]

    assert record["cleaned"]["counter_service"]["monday_to_friday"]["open"] == "09:00"


def test_build_import_summary_counts_statuses_and_issues(tmp_path):
    submissions = [
        _submission("processed-court", "processed"),
        _submission("warning-court", "processed_with_warnings", issue_code="INVALID_PHONE"),
        _submission("review-court", "needs_human_review", issue_code="DUPLICATE_COURT_SLUG"),
        _submission("failed-court", "failed", issue_code="MISSING_COURT_IDENTIFIER"),
    ]
    ingest_result = IngestResult(submissions=submissions, skipped_empty_rows=3)
    profile = _profile(tmp_path / "source.csv", row_count=7)

    summary = build_import_summary(
        submissions,
        ingest_result,
        profile,
        "run-1",
        vocabulary_source="fact_data_api",
        llm_enabled=True,
    )

    assert summary["run_id"] == "run-1"
    assert summary["source_file"].endswith("source.csv")
    assert summary["vocabulary_source"] == "fact_data_api"
    assert summary["llm_enabled"] is True
    assert summary["row_count"] == 7
    assert summary["submission_count"] == 4
    assert summary["processed_count"] == 1
    assert summary["processed_with_warnings_count"] == 1
    assert summary["needs_human_review_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["skipped_count"] == 3
    assert summary["duplicate_slug_group_count"] == 1
    assert summary["duplicate_slug_affected_record_count"] == 1
    assert summary["issue_counts_by_code"]["INVALID_PHONE"] == 1
    assert summary["llm_requested"] is False
    assert summary["llm_calls"] == 0
    assert summary["llm_model"] is None


def test_build_import_summary_includes_llm_usage_metrics(tmp_path):
    summary = build_import_summary(
        [_submission("processed-court", "processed")],
        IngestResult(),
        _profile(tmp_path / "source.csv", row_count=1),
        "run-1",
        llm_enabled=True,
        llm_requested=True,
        llm_metrics={
            "llm_calls": 3,
            "llm_failures": 1,
            "llm_retries": 1,
            "llm_fields_selected": 5,
            "llm_fields_processed": 4,
            "llm_submissions_with_selected_fields": 2,
            "llm_model": "gpt-5.5",
        },
    )

    assert summary["llm_requested"] is True
    assert summary["llm_calls"] == 3
    assert summary["llm_failures"] == 1
    assert summary["llm_fields_processed"] == 4
    assert summary["llm_model"] == "gpt-5.5"


def test_write_processing_outputs_writes_expected_files(tmp_path):
    submissions = [
        _submission("processed-court", "processed"),
        _submission("failed-court", "failed", issue_code="MISSING_COURT_IDENTIFIER"),
    ]
    ingest_result = IngestResult(submissions=submissions, skipped_empty_rows=1)
    profile = _profile(tmp_path / "source.csv", row_count=4)

    result = write_processing_outputs(
        submissions=submissions,
        ingest_result=ingest_result,
        workbook_profile=profile,
        output_path=tmp_path,
        run_id="run-1",
        vocabulary_source="local_json",
    )

    assert result.run_id == "run-1"
    assert (tmp_path / "fact_import_payload.json").exists()
    assert (tmp_path / "failed_records.json").exists()
    assert (tmp_path / "records_needing_human_review.json").exists()
    assert (tmp_path / "issue_report.json").exists()
    assert (tmp_path / "import_summary.json").exists()

    payload = json.loads((tmp_path / "fact_import_payload.json").read_text())
    summary = json.loads((tmp_path / "import_summary.json").read_text())
    assert payload["records"][0]["courtSlug"] == "processed-court"
    assert summary["failed_count"] == 1
    assert summary["vocabulary_source"] == "local_json"


def _submission(court_slug, status, issue_code=None):
    issues = []
    if issue_code:
        issues.append(
            Issue(
                field="court_slug",
                code=issue_code,
                severity="warning",
                message="Test issue",
            )
        )

    return CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug_raw=court_slug,
        court_slug=court_slug,
        status=status,
        issues=issues,
    )


def _profile(source_path, row_count):
    return WorkbookProfile(
        source_path=source_path,
        sheet_name=None,
        row_count=row_count,
        column_count=0,
        columns=[],
    )
