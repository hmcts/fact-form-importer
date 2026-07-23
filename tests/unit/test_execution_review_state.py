import pytest

from fact_form_importer.execution.review_state import (
    CourtTargetOverride,
    CollectionItemResolution,
    CourtDisposition,
    ExecutionReviewLedger,
    ExecutionReviewStore,
    build_target_comparison,
)


def test_merge_operations_update_and_create_while_preserving_unmatched_live_entries():
    action = {
        "action_id": "court-address",
        "resource": "address",
        "method": "POST",
        "path": "/courts/id/v1/address",
    }
    current = [
        {"id": "visit-id", "addressType": "Visit", "addressLine1": "Old"},
        {"id": "postal-id", "addressType": "Post", "addressLine1": "Surplus"},
    ]
    proposed = [
        {"addressType": "Visit", "addressLine1": "New"},
        {"addressType": "Other", "addressLine1": "Created"},
    ]

    comparison = build_target_comparison("court", {**action, "proposed_items": proposed}, current)
    operations = comparison.operations

    assert [operation["purpose"] for operation in operations] == ["create", "update"]
    assert all(operation["method"] != "DELETE" for operation in operations)
    assert {item["addressType"] for item in comparison.proposed} == {"Visit", "Post", "Other"}
    assert next(
        item for item in comparison.proposed if item["addressType"] == "Post"
    )["addressLine1"] == "Surplus"


def test_merge_preserves_blank_optional_fields_and_applies_explicit_clear():
    action = {
        "action_id": "court-contact",
        "resource": "contact_detail",
        "method": "POST",
        "path": "/courts/id/v1/contact-details",
        "proposed_items": [
            {"courtContactDescriptionId": "type", "phoneNumber": "020 7000 0000"}
        ],
        "proposed_item_clear_fields": [["explanation"]],
    }
    comparison = build_target_comparison(
        "court",
        action,
        [
            {
                "id": "contact-id",
                "courtContactDescriptionId": "type",
                "email": "help@example.test",
                "explanation": "Remove me",
            }
        ],
    )

    effective = comparison.proposed[0]
    assert effective["email"] == "help@example.test"
    assert effective["explanation"] is None
    assert comparison.operations[0]["body"]["explanation"] is None


def test_singleton_merge_preserves_unsubmitted_nested_professional_fields():
    action = {
        "action_id": "court-professional-information",
        "resource": "professional_information",
        "method": "POST",
        "path": "/courts/id/v1/professional-information",
        "body": {
            "professionalInformation": {
                "accessScheme": False,
                "interviewRoomCount": 7,
                "interviewRooms": True,
                "videoHearings": False,
            }
        },
    }
    comparison = build_target_comparison(
        "court",
        action,
        {
            "codes": {"familyCourtCode": 131},
            "professionalInformation": {
                "accessScheme": True,
                "commonPlatform": False,
                "interviewPhoneNumber": None,
                "interviewRoomCount": None,
                "interviewRoomCountConsistent": False,
                "interviewRooms": True,
                "videoHearings": True,
            },
        },
    )

    professional = comparison.proposed["professionalInformation"]
    assert comparison.proposed["codes"] == {"familyCourtCode": 131}
    assert professional["interviewRoomCount"] == 7
    assert professional["interviewRoomCountConsistent"] is False
    assert professional["interviewPhoneNumber"] is None
    assert comparison.operations[0]["body"]["professionalInformation"] == professional


def test_counter_service_merge_preserves_live_hours_when_submission_omits_them():
    action = {
        "action_id": "court-counter",
        "resource": "counter_service_opening_hours",
        "method": "PUT",
        "path": "/courts/id/v1/opening-hours/counter-service",
        "body": {
            "courtId": "id",
            "counterService": True,
            "assistWithForms": True,
            "assistWithDocuments": False,
            "assistWithSupport": False,
            "appointmentNeeded": False,
        },
    }
    current = {
        "counterService": True,
        "assistWithForms": False,
        "assistWithDocuments": False,
        "assistWithSupport": False,
        "appointmentNeeded": False,
        "openingTimesDetails": [
            {
                "dayOfWeek": "EVERYDAY",
                "openingTime": "09:00",
                "closingTime": "17:00",
            }
        ],
    }

    comparison = build_target_comparison("court", action, current)

    assert comparison.proposed["assistWithForms"] is True
    assert comparison.proposed["openingTimesDetails"] == current["openingTimesDetails"]


def test_singleton_explicit_clear_supports_nested_field_paths():
    action = {
        "action_id": "court-professional-information",
        "resource": "professional_information",
        "method": "POST",
        "path": "/courts/id/v1/professional-information",
        "body": {"professionalInformation": {"interviewRooms": True}},
        "clear_fields": ["professionalInformation.interviewPhoneNumber"],
    }

    comparison = build_target_comparison(
        "court",
        action,
        {
            "professionalInformation": {
                "interviewRooms": False,
                "interviewPhoneNumber": "020 7000 0000",
            }
        },
    )

    assert comparison.proposed["professionalInformation"] == {
        "interviewRooms": True,
        "interviewPhoneNumber": None,
    }


def test_merge_adds_required_zero_phone_only_when_live_and_submitted_values_are_missing():
    action = {
        "action_id": "court-accessibility",
        "resource": "accessibility_options",
        "method": "POST",
        "path": "/courts/id/v1/accessibility-options",
        "body": {"accessibleEntrance": False, "lift": False},
    }

    missing = build_target_comparison("court", action, {})
    preserved = build_target_comparison(
        "court",
        action,
        {
            "accessibleEntrancePhoneNumber": "020 7000 0001",
            "liftSupportPhoneNumber": "020 7000 0002",
        },
    )

    assert missing.proposed["accessibleEntrancePhoneNumber"] == "+44 0000000000"
    assert missing.proposed["liftSupportPhoneNumber"] == "+44 0000000000"
    assert preserved.proposed["accessibleEntrancePhoneNumber"] == "020 7000 0001"
    assert preserved.proposed["liftSupportPhoneNumber"] == "020 7000 0002"


def test_merge_blocks_ambiguous_business_type_matches():
    action = {
        "action_id": "court-contact",
        "resource": "contact_detail",
        "method": "POST",
        "path": "/courts/id/v1/contact-details",
        "proposed_items": [{"courtContactDescriptionId": "type", "explanation": "New"}],
    }
    comparison = build_target_comparison(
        "court",
        action,
        [
            {"id": "one", "courtContactDescriptionId": "type"},
            {"id": "two", "courtContactDescriptionId": "type"},
        ],
    )

    assert comparison.merge_conflicts
    assert comparison.operations == []


def test_target_approval_is_hash_bound_and_source_change_invalidates_it(tmp_path):
    store = ExecutionReviewStore(tmp_path)
    action = {
        "action_id": "court-contact",
        "resource": "contact_detail",
        "method": "POST",
        "path": "/courts/id/v1/contact-details",
        "source_row_number": 2,
        "proposed_items": [{"courtContactDescriptionId": "type", "explanation": "New"}],
    }
    comparison = build_target_comparison(
        "court", action, [{"id": "contact-id", "explanation": "Old"}]
    )

    store.save_comparison("run", comparison)
    approved = store.approve_target("run", comparison.change_id)
    repeated = store.approve_target("run", comparison.change_id)

    assert approved.target_approvals[comparison.change_id].approved_at == repeated.target_approvals[
        comparison.change_id
    ].approved_at
    store.select_source("run", "court", 2)
    assert store.load("run").comparisons == {}
    assert store.load("run").target_approvals == {}


def test_changed_live_snapshot_invalidates_previous_replacement_approval(tmp_path):
    store = ExecutionReviewStore(tmp_path)
    action = {
        "action_id": "court-facility",
        "resource": "building_facilities",
        "method": "POST",
        "path": "/courts/id/v1/building-facilities",
        "body": {"parking": True},
    }
    first = build_target_comparison("court", action, {"parking": False})
    store.save_comparison("run", first)
    store.approve_target("run", first.change_id)

    changed = build_target_comparison("court", action, {"parking": None})
    store.save_comparison("run", changed)

    assert changed.change_id not in store.load("run").target_approvals


def test_court_target_override_invalidates_only_comparisons_for_its_source_row(tmp_path):
    store = ExecutionReviewStore(tmp_path)
    first = build_target_comparison(
        "first-court",
        {
            "action_id": "first-action",
            "resource": "building_facilities",
            "body": {"parking": True},
            "source_row_number": 2,
        },
        {"parking": False},
    )
    second = build_target_comparison(
        "second-court",
        {
            "action_id": "second-action",
            "resource": "building_facilities",
            "body": {"parking": True},
            "source_row_number": 3,
        },
        {"parking": False},
    )
    store.save_comparisons("run", [first, second])
    store.approve_targets("run", {first.change_id, second.change_id})

    ledger = store.set_court_target_override(
        "run",
        CourtTargetOverride(
            source_row_number=2,
            submitted_slug="missing-court",
            target_slug="validated-court",
            target_court_id="court-id",
            target_court_name="Validated Court",
        ),
    )

    assert set(ledger.comparisons) == {second.change_id}
    assert set(ledger.target_approvals) == {second.change_id}
    assert ledger.court_target_overrides["2"].target_slug == "validated-court"


def test_bulk_comparison_save_preserves_unchanged_approval_and_invalidates_changed_one(
    tmp_path,
):
    store = ExecutionReviewStore(tmp_path)
    first_action = {
        "action_id": "court-first",
        "resource": "building_facilities",
        "method": "POST",
        "path": "/courts/id/v1/building-facilities",
        "body": {"parking": True},
    }
    second_action = {**first_action, "action_id": "court-second"}
    first = build_target_comparison("court", first_action, {"parking": False})
    second = build_target_comparison("court", second_action, {"parking": False})
    store.save_comparisons("run", [first, second])
    store.approve_target("run", first.change_id)
    store.approve_target("run", second.change_id)

    changed_second = build_target_comparison(
        "court", second_action, {"parking": None}
    )
    ledger = store.save_comparisons("run", [first, changed_second])

    assert first.change_id in ledger.target_approvals
    assert changed_second.change_id not in ledger.target_approvals


def test_bulk_target_approval_is_atomic_validated_and_idempotent(tmp_path):
    store = ExecutionReviewStore(tmp_path)
    action = {
        "action_id": "court-valid",
        "resource": "building_facilities",
        "method": "POST",
        "path": "/courts/id/v1/building-facilities",
        "body": {"parking": True},
    }
    valid = build_target_comparison("court", action, {"parking": False})
    empty = build_target_comparison(
        "court", {**action, "action_id": "court-empty"}, {}
    )
    store.save_comparisons("run", [valid, empty])

    with pytest.raises(ValueError, match="does not require"):
        store.approve_targets("run", {valid.change_id, empty.change_id})

    assert store.load("run").target_approvals == {}
    approved, added = store.approve_targets("run", {valid.change_id})
    repeated, repeated_added = store.approve_targets("run", {valid.change_id})
    assert added == 1
    assert repeated_added == 0
    assert approved.target_approvals[valid.change_id].approved_at == repeated.target_approvals[
        valid.change_id
    ].approved_at


def test_collection_merge_collapses_exact_and_complementary_contacts():
    action = {
        "action_id": "court-contacts",
        "resource": "contact_detail",
        "method": "POST",
        "path": "/courts/id/v1/contact-details",
        "proposed_items": [
            {"courtContactDescriptionId": "type-1", "phoneNumber": "020 1234 5678"},
            {"courtContactDescriptionId": "type-1", "email": "court@example.test"},
            {"courtContactDescriptionId": "type-2", "phoneNumber": "0300 123 4567"},
            {"courtContactDescriptionId": "type-2", "phoneNumber": "0300 123 4567"},
        ],
    }

    comparison = build_target_comparison("court", action, [])

    assert comparison.merge_conflicts == []
    assert len(comparison.proposed) == 2
    assert comparison.proposed[0]["phoneNumber"] == "020 1234 5678"
    assert comparison.proposed[0]["email"] == "court@example.test"


def test_collection_merge_keeps_conflicting_duplicate_contacts_blocked():
    action = {
        "action_id": "court-contacts",
        "resource": "contact_detail",
        "method": "POST",
        "path": "/courts/id/v1/contact-details",
        "proposed_items": [
            {"courtContactDescriptionId": "type-1", "phoneNumber": "020 1111 1111"},
            {"courtContactDescriptionId": "type-1", "phoneNumber": "020 2222 2222"},
        ],
    }

    comparison = build_target_comparison("court", action, [])

    assert comparison.operations == []
    assert comparison.merge_conflicts == [
        "Multiple contact detail entries use business type 'type-1'"
    ]


def test_review_store_recovers_valid_json_prefix_and_keeps_corrupt_original(tmp_path):
    store = ExecutionReviewStore(tmp_path)
    path = store.path_for("run")
    path.parent.mkdir(parents=True)
    path.write_text(
        ExecutionReviewLedger(run_id="run").model_dump_json() + " trailing corruption",
        encoding="utf-8",
    )

    recovered = store.load("run")

    assert recovered.run_id == "run"
    assert ExecutionReviewLedger.model_validate_json(path.read_text()).run_id == "run"
    assert list(path.parent.glob("run.json.corrupt-*"))


def test_item_resolution_and_court_disposition_invalidate_only_affected_comparisons(tmp_path):
    store = ExecutionReviewStore(tmp_path)
    first = build_target_comparison(
        "first",
        {
            "action_id": "first-action",
            "resource": "building_facilities",
            "source_row_number": 2,
            "body": {"parking": True},
        },
        {"parking": False},
    )
    second = build_target_comparison(
        "second",
        {
            "action_id": "second-action",
            "resource": "building_facilities",
            "source_row_number": 3,
            "body": {"parking": True},
        },
        {"parking": False},
    )
    store.save_comparisons("run", [first, second])
    store.approve_targets("run", {first.change_id, second.change_id})

    resolved = store.resolve_collection_items(
        "run",
        [
            CollectionItemResolution(
                item_id=item_id,
                action_id="first-action",
                resource="contact_detail",
                decision="omit",
                rationale="Duplicate entry",
            )
            for item_id in ("item-1", "item-2")
        ],
    )
    closed = store.close_court(
        "run",
        CourtDisposition(
            source_row_number=3,
            court_slug="second",
            rationale="No defensible court target",
        ),
    )

    assert first.change_id not in resolved.comparisons
    assert second.change_id in resolved.comparisons
    assert closed.comparisons == {}
    assert closed.collection_item_resolutions["item-1"].rationale == "Duplicate entry"
    assert closed.collection_item_resolutions["item-2"].decision == "omit"
    assert closed.court_dispositions["3"].status == "unactionable"
