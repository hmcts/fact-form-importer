from fact_form_importer.models.court_submission import (
    Address,
    ContactDetail,
    CourtSubmission,
    OpeningHoursSet,
    OpeningTime,
)
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.output.fact_api_manifest import build_fact_api_import_manifest
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.validators.vocabularies import Vocabularies


def test_manifest_builds_ready_actions_with_preflight_and_source_evidence():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        status="processed",
        facilities={
            "accessible_parking": True,
            "accessible_toilet_description": "Ground floor",
            "accessible_entrance": True,
            "accessible_entrance_support_phone": "020 7946 0000",
            "hearing_enhancement_equipment": "Hearing loop systems are available at this court.",
            "lift_available": True,
            "lift_door_width": "90",
            "lift_weight_limit": "1000",
            "quiet_room_available": True,
            "quiet_room_available_2": False,
            "parking_available": True,
            "food_and_drink": ["Free water dispensers"],
            "separate_waiting_areas": False,
            "child_waiting_area": False,
            "baby_changing": False,
            "wifi_available": True,
        },
        translation_email="translation@example.test",
        interview_rooms={"has_interview_rooms": True, "room_count": "2"},
        counter_service={
            "assists_with": ["Forms"],
            "same_monday_to_friday": True,
            "monday_to_friday": OpeningTime(open="09:00", close="17:00", status="valid_time"),
        },
        addresses=[
            Address(
                index=1,
                address_type="Visit",
                line_1="1 Main Street",
                town_or_city="London",
                postcode="SW1A 1AA",
            )
        ],
        contacts=[ContactDetail(index=1, description="Enquiries", email="contact@example.test")],
        opening_hours=[
            OpeningHoursSet(
                index=1,
                type="Court open",
                same_monday_to_friday=True,
                monday_to_friday=OpeningTime(open="09:00", close="17:00", status="valid_time"),
            )
        ],
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", _vocabularies(), lambda slug: CourtReference("court-id", slug)
    ).manifest

    record = manifest.records[0]
    actions = {action.resource: action for action in record.actions}
    assert record.court_id == "court-id"
    assert actions["building_facilities"].readiness == "ready"
    assert actions["building_facilities"].body["courtId"] == "court-id"
    assert actions["accessibility_options"].body["quietRoom"] is True
    assert actions["building_facilities"].body["quietRoom"] is False
    assert actions["accessibility_options"].body["accessibleEntrancePhoneNumber"] == "020 7946 0000"
    assert actions["counter_service_opening_hours"].body["counterService"] is True
    assert actions["counter_service_opening_hours"].body["openingTimesDetails"][0]["dayOfWeek"] == "EVERYDAY"
    assert actions["address"].readiness == "ready"
    assert actions["address"].preflight_required is True
    assert actions["address"].source_fields == ["addresses[1]"]
    assert actions["contact_detail"].body["courtContactDescriptionId"] == "contact-id"
    assert actions["court_opening_hours"].body["openingHourTypeId"] == "opening-id"
    assert actions["professional_information"].readiness == "pending"
    assert "videoHearings" in actions["professional_information"].reason
    assert manifest.summary["api_manifest_ready_action_count"] == 7
    assert manifest.summary["api_manifest_pending_action_count"] == 1


def test_manifest_marks_invalid_api_text_and_missing_court_uuid_as_pending():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        status="processed",
        facilities={"accessible_toilet_description": "Toilet at reception / ask staff"},
    )

    manifest = build_fact_api_import_manifest([submission], "run-1", _vocabularies()).manifest

    action = manifest.records[0].actions[0]
    assert action.readiness == "pending"
    assert "UUID" in action.reason
    assert "characters rejected" in action.reason


def test_manifest_excludes_non_importable_records():
    review = CourtSubmission(
        source=SourceMetadata(source_row_number=2), court_slug="review-court", status="needs_human_review"
    )

    manifest = build_fact_api_import_manifest([review], "run-1", _vocabularies()).manifest

    assert manifest.records == []
    assert manifest.summary["api_manifest_record_count"] == 0


def test_manifest_keeps_unknown_child_values_pending_and_supports_weekday_times():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=4),
        court_slug="example-court",
        status="processed_with_warnings",
        addresses=[
            Address(index=1, address_type="Unknown", line_1="1 Main Street", areas_of_law=["Unknown area"])
        ],
        contacts=[ContactDetail(index=1, description="Unknown contact", explanation="bad/slash")],
        opening_hours=[
            OpeningHoursSet(
                index=1,
                type="Unknown hours",
                monday=OpeningTime(open="09:00", close="17:00", status="valid_time"),
                tuesday=OpeningTime(open="09:00", close="17:00", status="known_text_status"),
            )
        ],
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", _vocabularies(), lambda slug: CourtReference("court-id", slug)
    ).manifest
    actions = {action.resource: action for action in manifest.records[0].actions}

    assert "not recognised" in actions["address"].reason
    assert "not in the" in actions["contact_detail"].reason
    assert actions["contact_detail"].readiness == "pending"
    assert actions["court_opening_hours"].body["openingTimesDetails"] == [
        {"dayOfWeek": "MONDAY", "openingTime": "09:00", "closingTime": "17:00"}
    ]
    assert "UUID" in actions["court_opening_hours"].reason


def test_manifest_marks_contact_api_constraint_and_empty_record_states():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=5),
        court_slug="example-court",
        status="processed",
        contacts=[ContactDetail(index=1, explanation="x" * 256)],
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", _vocabularies(), lambda slug: CourtReference("court-id", slug)
    ).manifest

    action = manifest.records[0].actions[0]
    assert action.readiness == "pending"
    assert "maximum length" in action.reason
    assert manifest.records[0].readiness == "pending"


def test_manifest_marks_api_required_conditional_values_as_pending():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=6),
        court_slug="example-court",
        status="processed",
        facilities={
            "accessible_parking": True,
            "accessible_entrance": False,
            "hearing_enhancement_equipment": "Hearing loop systems are available at this court.",
            "lift_available": False,
            "quiet_room_available": True,
        },
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", _vocabularies(), lambda slug: CourtReference("court-id", slug)
    ).manifest

    action = manifest.records[0].actions[0]
    assert action.resource == "accessibility_options"
    assert action.readiness == "pending"
    assert "accessibleEntrancePhoneNumber" in action.reason
    assert "liftSupportPhoneNumber" in action.reason


def _vocabularies():
    return Vocabularies(
        version="test",
        vocabularies={
            "areas_of_law": [{"code": "civil", "name": "Civil", "api_id": "area-id"}],
            "court_types": [{"code": "county", "name": "County Court", "api_id": "type-id"}],
            "opening_hour_types": [{"code": "court_open", "name": "Court open", "api_id": "opening-id"}],
            "contact_description_types": [{"code": "enquiries", "name": "Enquiries", "api_id": "contact-id"}],
        },
    )
