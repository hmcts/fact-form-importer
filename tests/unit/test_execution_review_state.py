from fact_form_importer.execution.review_state import (
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

    assert missing.proposed["accessibleEntrancePhoneNumber"] == "00000000000"
    assert missing.proposed["liftSupportPhoneNumber"] == "00000000000"
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
