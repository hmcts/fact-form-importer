from fact_form_importer.models.court_submission import (
    Address,
    ContactDetail,
    CourtSubmission,
    OpeningHoursSet,
    OpeningTime,
)
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.models.issues import Issue
from fact_form_importer.output.fact_api_manifest import (
    build_fact_api_import_manifest,
    normalise_fact_api_action_body,
    validate_fact_api_action_body,
)
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.validators.os_addresses import (
    AddressVerification,
    AddressVerificationBatch,
    OsAddressCandidate,
)
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
    assert (
        actions["counter_service_opening_hours"].body["openingTimesDetails"][0]["dayOfWeek"]
        == "EVERYDAY"
    )
    assert actions["address"].readiness == "ready"
    assert actions["address"].preflight_required is True
    assert actions["address"].source_fields == ["addresses[1]"]
    assert actions["contact_detail"].body["courtContactDescriptionId"] == "contact-id"
    assert actions["court_opening_hours"].body["openingHourTypeId"] == "opening-id"
    professional_information = actions["professional_information"]
    assert professional_information.readiness == "ready"
    assert professional_information.body["professionalInformation"] == {
        "interviewRooms": True,
        "interviewRoomCount": 2,
        "videoHearings": False,
        "commonPlatform": False,
        "accessScheme": False,
    }
    assert professional_information.migration_assumptions == [
        "Migration policy: the form does not collect videoHearings, commonPlatform, "
        "or accessScheme, so this request defaults each field to false."
    ]
    assert manifest.summary["api_manifest_ready_action_count"] == 8
    assert manifest.summary["api_manifest_pending_action_count"] == 0


def test_manifest_marks_invalid_api_text_pending_and_resolves_court_uuid_at_execution():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        status="processed",
        facilities={"accessible_toilet_description": "Toilet at reception / ask staff"},
    )

    manifest = build_fact_api_import_manifest([submission], "run-1", _vocabularies()).manifest

    action = manifest.records[0].actions[0]
    assert action.readiness == "pending"
    assert "UUID" not in action.reason
    assert action.body["courtId"] == "{court_id}"
    assert "characters rejected" in action.reason


def test_manifest_excludes_non_importable_records():
    review = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="review-court",
        status="needs_human_review",
    )

    manifest = build_fact_api_import_manifest([review], "run-1", _vocabularies()).manifest

    assert manifest.records == []
    assert manifest.summary["api_manifest_record_count"] == 0


def test_manifest_includes_llm_only_review_row_and_attaches_field_dependency():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        status="needs_human_review",
        facilities={"accessible_toilet_description": "Near reception."},
        issues=[
            Issue(
                field="facilities.accessible_toilet_description",
                code="LLM_LOW_CONFIDENCE",
                severity="warning",
                message="Review",
            )
        ],
    )
    review_item = {
        "source_row_number": 2,
        "field": "facilities.accessible_toilet_description",
        "outcome": "accepted",
    }

    manifest = build_fact_api_import_manifest(
        [submission],
        "run-1",
        _vocabularies(),
        lambda slug: CourtReference("court-id", slug),
        llm_review_items=[review_item],
    ).manifest

    action = next(
        action
        for action in manifest.records[0].actions
        if action.resource == "accessibility_options"
    )
    assert action.llm_review_ids
    assert manifest.summary["api_manifest_awaiting_llm_approval_action_count"] == 1


def test_manifest_turns_usable_llm_address_selection_into_an_approval_dependency():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        status="processed_with_warnings",
        addresses=[
            Address(
                index=1,
                address_type="Visit",
                line_1="St Mary's Court",
                line_2="Regents Park Road",
                town_or_city="London",
                postcode="N3 1BQ",
            )
        ],
    )
    candidate = OsAddressCandidate(
        uprn="200222235",
        address=None,
        organisation_name="BARNET COUNTY COURT",
        building_number=None,
        building_name="ST. MARYS COURT",
        thoroughfare_name="REGENTS PARK ROAD",
        post_town="LONDON",
        postcode="N3 1BQ",
    )
    verification = AddressVerification(
        source_row_number=2,
        court_slug="example-court",
        address_index=1,
        postcode="N3 1BQ",
        status="review_required",
        message="Deterministic match was not high enough",
        original_address=submission.addresses[0].model_dump(mode="json"),
        candidates=[candidate],
        llm_suggestion={
            "uprn": "200222235",
            "confidence": "high",
            "needs_human_review": False,
            "reason": "One consistent candidate",
        },
    )

    manifest = build_fact_api_import_manifest(
        [submission],
        "run-1",
        _vocabularies(),
        lambda slug: CourtReference("court-id", slug),
        AddressVerificationBatch(enabled=True, verifications=[verification]),
    ).manifest

    action = next(action for action in manifest.records[0].actions if action.resource == "address")
    assert action.readiness == "ready"
    assert action.reason is None
    assert action.llm_review_ids


def test_manifest_plans_a_valid_section_despite_an_unrelated_source_error():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        status="needs_human_review",
        addresses=[
            Address(
                index=1,
                address_type="Visit",
                line_1="1 Main Street",
                town_or_city="London",
                postcode="SW1A 1AA",
            )
        ],
        issues=[
            Issue(
                field="contacts[1].email",
                code="INVALID_EMAIL",
                severity="error",
                message="Contact email is invalid",
            )
        ],
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", _vocabularies(), lambda slug: CourtReference("court-id", slug)
    ).manifest

    assert [action.resource for action in manifest.records[0].actions] == ["address"]
    assert manifest.records[0].actions[0].readiness == "ready"


def test_duplicate_court_rows_have_provisional_actions_pending_source_selection():
    submissions = [
        CourtSubmission(
            source=SourceMetadata(source_row_number=row),
            court_slug="duplicate-court",
            contacts=[ContactDetail(index=1, description="Enquiries", phone=f"020 7000 000{row}")],
        )
        for row in (2, 3)
    ]

    manifest = build_fact_api_import_manifest(
        submissions,
        "run-1",
        _vocabularies(),
        lambda slug: CourtReference("court-id", slug),
    ).manifest

    record = manifest.records[0]
    assert record.source_row_numbers == [2, 3]
    assert len(record.actions) == 2
    assert all(action.source_selection_required for action in record.actions)
    assert {action.source_row_number for action in record.actions} == {2, 3}


def test_po_box_address_has_no_special_manual_value_dependency():
    address = Address(
        index=1,
        address_type="Visit",
        line_1="PO Box 12",
        town_or_city="London",
        postcode="SW1A 1AA",
    )
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=4),
        court_slug="example-court",
        addresses=[address],
    )
    verification = AddressVerification(
        source_row_number=4,
        court_slug="example-court",
        address_index=1,
        postcode="SW1A 1AA",
        status="unavailable",
        message="Address lookup is temporarily unavailable",
        original_address=address.model_dump(mode="json"),
    )

    manifest = build_fact_api_import_manifest(
        [submission],
        "run-1",
        _vocabularies(),
        lambda slug: CourtReference("court-id", slug),
        AddressVerificationBatch(enabled=True, verifications=[verification]),
    ).manifest

    action = manifest.records[0].actions[0]
    assert action.readiness == "ready"
    assert action.llm_review_ids == []
    assert action.body["addressLine1"] == "PO Box 12"


def test_manifest_omits_professional_information_without_form_evidence():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        status="processed",
        interview_rooms={
            "has_interview_rooms": None,
            "room_count": None,
            "booking_phone": None,
        },
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", _vocabularies(), lambda slug: CourtReference("court-id", slug)
    ).manifest

    assert manifest.records == []


def test_manifest_keeps_unknown_child_values_pending_and_supports_weekday_times():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=4),
        court_slug="example-court",
        status="processed_with_warnings",
        addresses=[
            Address(
                index=1,
                address_type="Unknown",
                line_1="1 Main Street",
                areas_of_law=["Unknown area"],
            )
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


def test_manifest_uses_review_visible_defaults_for_blank_lift_measurements():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=7),
        court_slug="example-court",
        status="processed",
        facilities={
            "accessible_parking": True,
            "accessible_entrance": True,
            "hearing_enhancement_equipment": "Hearing loop systems are available at this court.",
            "lift_available": True,
            "quiet_room_available": False,
        },
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", _vocabularies(), lambda slug: CourtReference("court-id", slug)
    ).manifest

    action = next(
        action
        for action in manifest.records[0].actions
        if action.resource == "accessibility_options"
    )
    assert action.readiness == "ready"
    assert action.body["liftDoorWidth"] == 1
    assert action.body["liftDoorLimit"] == 1
    assert action.migration_assumptions == [
        "Review-required migration default: lift is marked available but the source has no "
        "door width, so this FaCT request uses 1 cm. It does not amend the source or cleaned data.",
        "Review-required migration default: lift is marked available but the source has no "
        "weight limit, so this FaCT request uses 1 kg. It does not amend the source or cleaned data.",
    ]
    assert manifest.summary["api_manifest_review_required_default_count"] == 2
    assert manifest.summary["api_manifest_review_required_default_action_count"] == 1


def test_manifest_normalises_explicit_lift_measurement_units():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=8),
        court_slug="croydon-employment-tribunal",
        status="processed",
        facilities={
            "accessible_parking": False,
            "accessible_toilet_description": "Available on the first floor.",
            "accessible_entrance": True,
            "hearing_enhancement_equipment": "Infrared systems are available at this court.",
            "lift_available": True,
            "lift_door_width": "800 mm",
            "lift_weight_limit": "650KG",
            "quiet_room_available": False,
        },
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", _vocabularies(), lambda slug: CourtReference("court-id", slug)
    ).manifest

    action = next(
        action
        for action in manifest.records[0].actions
        if action.resource == "accessibility_options"
    )
    assert action.readiness == "ready"
    assert action.reason is None
    assert action.body["liftDoorWidth"] == 80
    assert action.body["liftDoorLimit"] == 650
    assert action.migration_assumptions == []


def test_manifest_does_not_default_explicitly_invalid_or_missing_lift_answers():
    explicit_invalid = CourtSubmission(
        source=SourceMetadata(source_row_number=8),
        court_slug="invalid-lift-court",
        status="processed",
        facilities={
            "accessible_parking": True,
            "accessible_entrance": True,
            "hearing_enhancement_equipment": "Hearing loop systems are available at this court.",
            "lift_available": True,
            "lift_door_width": "0",
            "lift_weight_limit": "not recorded",
            "quiet_room_available": False,
        },
    )
    missing_controller = CourtSubmission(
        source=SourceMetadata(source_row_number=9),
        court_slug="missing-lift-court",
        status="processed",
        facilities={
            "accessible_parking": True,
            "accessible_entrance": True,
            "hearing_enhancement_equipment": "Hearing loop systems are available at this court.",
            "quiet_room_available": False,
        },
    )

    manifest = build_fact_api_import_manifest(
        [explicit_invalid, missing_controller],
        "run-1",
        _vocabularies(),
        lambda slug: CourtReference(f"{slug}-id", slug),
    ).manifest
    invalid_action = next(
        action
        for action in manifest.records[0].actions
        if action.resource == "accessibility_options"
    )
    missing_action = next(
        action
        for action in manifest.records[1].actions
        if action.resource == "accessibility_options"
    )

    assert invalid_action.readiness == "pending"
    assert invalid_action.body.get("liftDoorWidth") is None
    assert invalid_action.body.get("liftDoorLimit") is None
    assert invalid_action.migration_assumptions == []
    assert "liftDoorWidth" in invalid_action.reason
    assert "liftDoorLimit" in invalid_action.reason
    assert missing_action.readiness == "pending"
    assert "lift" not in missing_action.body
    assert "lift is required" in missing_action.reason


def test_manifest_uses_review_visible_interview_room_count_defaults():
    rooms_available = CourtSubmission(
        source=SourceMetadata(source_row_number=10),
        court_slug="rooms-available-court",
        status="processed",
        interview_rooms={"has_interview_rooms": True},
    )
    rooms_unavailable = CourtSubmission(
        source=SourceMetadata(source_row_number=11),
        court_slug="rooms-unavailable-court",
        status="processed",
        interview_rooms={"has_interview_rooms": False, "room_count": "3"},
    )
    unknown_answer = CourtSubmission(
        source=SourceMetadata(source_row_number=12),
        court_slug="rooms-unknown-court",
        status="processed",
        interview_rooms={"room_count": "3"},
    )

    manifest = build_fact_api_import_manifest(
        [rooms_available, rooms_unavailable, unknown_answer],
        "run-1",
        _vocabularies(),
        lambda slug: CourtReference(f"{slug}-id", slug),
    ).manifest
    actions = {
        record.court_slug: next(
            action for action in record.actions if action.resource == "professional_information"
        )
        for record in manifest.records
    }

    assert actions["rooms-available-court"].readiness == "ready"
    assert (
        actions["rooms-available-court"].body["professionalInformation"]["interviewRoomCount"] == 1
    )
    assert "uses 1" in actions["rooms-available-court"].migration_assumptions[1]
    assert actions["rooms-unavailable-court"].readiness == "ready"
    assert (
        actions["rooms-unavailable-court"].body["professionalInformation"]["interviewRoomCount"]
        == 0
    )
    assert "uses a room count of 0" in actions["rooms-unavailable-court"].migration_assumptions[1]
    assert actions["rooms-unknown-court"].readiness == "pending"
    assert "interviewRooms is required" in actions["rooms-unknown-court"].reason
    assert manifest.summary["api_manifest_review_required_default_count"] == 2
    assert manifest.summary["api_manifest_review_required_default_action_count"] == 2


def test_manifest_normalises_conventional_address_notation_for_fact_api():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=7),
        court_slug="example-court",
        status="processed",
        addresses=[
            Address(
                index=1,
                address_type="Visit",
                line_1="Court C/o Service & Support",
                town_or_city="Town & City",
                postcode="SW1A 1AA",
            )
        ],
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", _vocabularies(), lambda slug: CourtReference("court-id", slug)
    ).manifest

    action = manifest.records[0].actions[0]
    assert action.readiness == "ready"
    assert action.body["addressLine1"] == "Court care of Service and Support"
    assert action.body["townCity"] == "Town and City"


def test_fact_api_contract_validation_blocks_known_unrepresentable_values():
    assert (
        normalise_fact_api_action_body("address", {"addressLine1": "C/o Court & Tribunal"})[
            "addressLine1"
        ]
        == "care of Court and Tribunal"
    )

    address_reason = validate_fact_api_action_body(
        "address",
        {
            "courtId": "court-id",
            "addressLine1": "1 Main Street",
            "townCity": "Edinburgh",
            "postcode": "EH1 1AA",
            "addressType": "VISIT_US",
        },
    )
    assert "Scotland" in address_reason

    contact_reason = validate_fact_api_action_body(
        "contact_detail",
        {
            "courtId": "court-id",
            "courtContactDescriptionId": "description-id",
            "phoneNumber": "123456789",
            "email": "invalid&mail@example.test",
        },
    )
    assert "phoneNumber" in contact_reason
    assert "email" in contact_reason

    empty_times_reason = validate_fact_api_action_body(
        "counter_service_opening_hours",
        {
            "courtId": "court-id",
            "counterService": True,
            "assistWithForms": False,
            "assistWithDocuments": False,
            "assistWithSupport": False,
            "appointmentNeeded": False,
        },
    )
    assert "openingTimesDetails" in empty_times_reason

    invalid_time_order_reason = validate_fact_api_action_body(
        "court_opening_hours",
        {
            "courtId": "court-id",
            "openingHourTypeId": "opening-id",
            "openingTimesDetails": [
                {"dayOfWeek": "EVERYDAY", "openingTime": "00:00", "closingTime": "00:00"}
            ],
        },
    )
    assert "before its closing time" in invalid_time_order_reason


def test_fact_api_contract_validation_covers_remaining_api_constraints():
    waiting_area_reason = validate_fact_api_action_body(
        "building_facilities",
        {
            "courtId": "court-id",
            "parking": False,
            "freeWaterDispensers": False,
            "snackVendingMachines": False,
            "drinkVendingMachines": False,
            "cafeteria": False,
            "waitingArea": True,
            "quietRoom": False,
            "babyChanging": False,
            "wifi": False,
        },
    )
    assert "waitingAreaChildren" in waiting_area_reason

    lift_reason = validate_fact_api_action_body(
        "accessibility_options",
        {
            "courtId": "court-id",
            "accessibleParking": False,
            "accessibleEntrance": True,
            "hearingEnhancementEquipment": "HEARING_LOOP",
            "lift": True,
            "quietRoom": False,
        },
    )
    assert "liftDoorWidth" in lift_reason
    assert "liftDoorLimit" in lift_reason

    invalid_support_phone_reason = validate_fact_api_action_body(
        "accessibility_options",
        {
            "courtId": "court-id",
            "accessibleParking": False,
            "accessibleEntrance": False,
            "accessibleEntrancePhoneNumber": "ask reception",
            "hearingEnhancementEquipment": "HEARING_LOOP_SYSTEMS",
            "lift": True,
            "liftDoorWidth": 1,
            "liftDoorLimit": 1,
            "quietRoom": False,
        },
    )
    assert (
        "accessibleEntrancePhoneNumber does not match the FaCT API phone format"
        in invalid_support_phone_reason
    )

    professional_reason = validate_fact_api_action_body("professional_information", {})
    assert "professionalInformation" in professional_reason


def test_professional_information_validation_enforces_interview_room_conditions():
    missing_count = validate_fact_api_action_body(
        "professional_information",
        {
            "professionalInformation": {
                "interviewRooms": True,
                "videoHearings": False,
                "commonPlatform": False,
                "accessScheme": False,
            }
        },
    )
    unexpected_count = validate_fact_api_action_body(
        "professional_information",
        {
            "professionalInformation": {
                "interviewRooms": False,
                "interviewRoomCount": 2,
                "videoHearings": False,
                "commonPlatform": False,
                "accessScheme": False,
            }
        },
    )

    assert "interviewRoomCount must be between 1 and 150" in missing_count
    assert "interviewRoomCount must be omitted or zero" in unexpected_count

    invalid_address_reason = validate_fact_api_action_body(
        "address",
        {
            "courtId": "court-id",
            "addressLine1": "Court / Building",
            "townCity": "T" * 101,
            "postcode": "BT1 1AA",
            "addressType": "VISIT_US",
        },
    )
    assert "addressLine1" in invalid_address_reason
    assert "townCity exceeds" in invalid_address_reason
    assert "Northern Ireland" in invalid_address_reason
    assert "Channel Islands" in validate_fact_api_action_body(
        "address",
        {
            "courtId": "court-id",
            "addressLine1": "1 Main Street",
            "townCity": "St Helier",
            "postcode": "JE1 1AA",
            "addressType": "VISIT_US",
        },
    )
    assert "must contain a space" in validate_fact_api_action_body(
        "address",
        {
            "courtId": "court-id",
            "addressLine1": "1 Main Street",
            "townCity": "London",
            "postcode": "SW1A1AA",
            "addressType": "VISIT_US",
        },
    )

    malformed_times_reason = validate_fact_api_action_body(
        "court_opening_hours",
        {
            "courtId": "court-id",
            "openingHourTypeId": "opening-id",
            "openingTimesDetails": [
                "not a detail",
                {"dayOfWeek": "NOT_A_DAY", "openingTime": "nine", "closingTime": "five"},
                {"dayOfWeek": "EVERYDAY", "openingTime": "09:00", "closingTime": "17:00"},
                {"dayOfWeek": "MONDAY", "openingTime": "09:00", "closingTime": "17:00"},
                {"dayOfWeek": "MONDAY", "openingTime": "09:00", "closingTime": "17:00"},
            ],
        },
    )
    assert "invalid opening period" in malformed_times_reason
    assert "invalid day" in malformed_times_reason
    assert "duplicate day" in malformed_times_reason
    assert "invalid opening time" in malformed_times_reason
    assert "invalid closing time" in malformed_times_reason
    assert "sole day" in malformed_times_reason


def test_manifest_marks_vocabulary_entries_without_api_ids_as_pending():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=8),
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
            )
        ],
    )
    vocabularies = Vocabularies(
        version="test",
        vocabularies={"areas_of_law": [{"code": "civil", "name": "Civil"}]},
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", vocabularies, lambda slug: CourtReference("court-id", slug)
    ).manifest

    assert "does not have a FaCT API UUID" in manifest.records[0].actions[0].reason


def test_manifest_keeps_ambiguous_os_address_evidence_and_blocks_only_that_address_action():
    address = Address(
        index=1,
        address_type="Visit",
        line_1="1 Main Street",
        town_or_city="London",
        postcode="SW1A 1AA",
    )
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=8),
        court_slug="example-court",
        status="processed_with_warnings",
        addresses=[address],
    )
    batch = AddressVerificationBatch(
        enabled=True,
        verifications=[
            AddressVerification(
                source_row_number=8,
                court_slug="example-court",
                address_index=1,
                postcode="SW1A 1AA",
                status="review_required",
                message="No unique high-confidence OS match was found",
                original_address=address.model_dump(mode="json"),
            )
        ],
    )

    manifest = build_fact_api_import_manifest(
        [submission],
        "run-1",
        _vocabularies(),
        lambda slug: CourtReference("court-id", slug),
        address_verifications=batch,
    ).manifest

    action = next(action for action in manifest.records[0].actions if action.resource == "address")
    assert action.readiness == "pending"
    assert "Address verification requires review" in action.reason
    assert action.address_verification["status"] == "review_required"


def test_manifest_normalises_contact_explanation_to_the_fact_api_charset():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=9),
        court_slug="example-court",
        status="processed",
        contacts=[
            ContactDetail(
                index=1, description="Enquiries", explanation="Civil / family: ask staff."
            )
        ],
    )

    manifest = build_fact_api_import_manifest(
        [submission], "run-1", _vocabularies(), lambda slug: CourtReference("court-id", slug)
    ).manifest

    action = next(
        action for action in manifest.records[0].actions if action.resource == "contact_detail"
    )
    assert action.readiness == "ready"
    assert action.body["explanation"] == "Civil family ask staff"
    assert action.request_body_normalisations["explanation"] == {
        "from": "Civil / family: ask staff.",
        "to": "Civil family ask staff",
    }


def _vocabularies():
    return Vocabularies(
        version="test",
        vocabularies={
            "areas_of_law": [{"code": "civil", "name": "Civil", "api_id": "area-id"}],
            "court_types": [{"code": "county", "name": "County Court", "api_id": "type-id"}],
            "opening_hour_types": [
                {"code": "court_open", "name": "Court open", "api_id": "opening-id"}
            ],
            "contact_description_types": [
                {"code": "enquiries", "name": "Enquiries", "api_id": "contact-id"}
            ],
        },
    )
