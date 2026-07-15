import json

from fact_form_importer.execution.overlay import derive_latest_execution_overlay
from fact_form_importer.models.court_submission import (
    Address,
    ContactDetail,
    CourtSubmission,
    OpeningHoursSet,
    OpeningTime,
)
from fact_form_importer.models.source import SourceMetadata


def test_overlay_derives_section_plan_and_preserves_succeeded_legacy_sections(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        status="processed",
        addresses=[
            Address(
                index=1,
                address_type="Visit",
                line_1="1 Main Street",
                town_or_city="London",
                postcode="SW1A 1AA",
                areas_of_law=["Civil"],
                court_types=["County Court"],
            )
        ],
        contacts=[
            ContactDetail(index=1, description="Enquiries", phone="020 7000 0000")
        ],
        opening_hours=[
            OpeningHoursSet(
                index=1,
                type="Court open",
                same_monday_to_friday=True,
                monday_to_friday=OpeningTime(
                    open="09:00", close="17:00", status="valid_time"
                ),
            )
        ],
        counter_service={
            "assists_with": ["Forms"],
            "specific_courts": ["County Court"],
            "same_monday_to_friday": True,
            "monday_to_friday": {
                "open": "09:00",
                "close": "17:00",
                "status": "valid_time",
            },
        },
    )
    (archive / "submissions_cleaned.json").write_text(
        json.dumps([submission.model_dump(mode="json")]), encoding="utf-8"
    )
    (archive / "address_verification_report.json").write_text("{}", encoding="utf-8")
    original = {
        "manifest_version": "1.6",
        "records": [
            {
                "court_slug": "example-court",
                "court_id": "court-id",
                "source_row_numbers": [2],
                "actions": [
                    {
                        "action_id": "legacy-address",
                        "resource": "address",
                        "method": "POST",
                        "path": "/courts/court-id/v1/address",
                        "source_fields": ["addresses[1]"],
                        "body": {
                            "areasOfLaw": ["area-id"],
                            "courtTypes": ["court-type-id"],
                        },
                    },
                    {
                        "action_id": "legacy-contact",
                        "resource": "contact_detail",
                        "source_fields": ["contacts[1]"],
                        "body": {"courtContactDescriptionId": "contact-id"},
                    },
                    {
                        "action_id": "legacy-hours",
                        "resource": "court_opening_hours",
                        "source_fields": ["opening_hours[1]"],
                        "body": {"openingHourTypeId": "hours-id"},
                    },
                    {
                        "action_id": "legacy-counter",
                        "resource": "counter_service_opening_hours",
                        "source_fields": ["counter_service"],
                        "body": {"courtTypes": ["court-type-id"]},
                    },
                ],
            },
            {
                "court_slug": "already-written-court",
                "court_id": "other-id",
                "source_row_numbers": [99],
                "actions": [
                    {
                        "action_id": "legacy-orphan",
                        "resource": "building_facilities",
                        "method": "POST",
                        "path": "/courts/other-id/v1/building-facilities",
                        "body": {"parking": True},
                    }
                ],
            },
        ],
    }

    overlay = derive_latest_execution_overlay(
        "run", archive, tmp_path / "out", original, {"legacy-address", "legacy-orphan"}
    )
    cached = derive_latest_execution_overlay(
        "run", archive, tmp_path / "out", original, {"legacy-address", "legacy-orphan"}
    )

    assert overlay["manifest_version"] == "1.9"
    assert overlay["derived_execution_overlay"] is True
    assert overlay["preserved_succeeded_section_count"] == 2
    example = next(
        record for record in overlay["records"] if record["court_slug"] == "example-court"
    )
    assert any(action["action_id"] == "legacy-address" for action in example["actions"])
    assert any(
        record["court_slug"] == "already-written-court" for record in overlay["records"]
    )
    assert cached == overlay


def test_overlay_falls_back_for_empty_or_abbreviated_legacy_evidence(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    original = {"manifest_version": "1.0", "records": [{"court_slug": "legacy"}]}

    assert derive_latest_execution_overlay("empty", archive, tmp_path, original) == original
    (archive / "submissions_cleaned.json").write_text(
        json.dumps([{"source": {"source_row_number": 2}, "issues": [{"code": "short"}]}]),
        encoding="utf-8",
    )
    assert derive_latest_execution_overlay("short", archive, tmp_path, original) == original
