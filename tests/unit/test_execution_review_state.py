from fact_form_importer.execution.review_state import (
    ExecutionReviewStore,
    build_target_comparison,
    replacement_operations,
)


def test_replacement_operations_update_and_create_before_deleting_surplus():
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

    operations = replacement_operations(action, current, proposed)

    assert [operation["purpose"] for operation in operations] == [
        "create",
        "update",
        "delete_surplus",
    ]
    assert operations[-1]["path"].endswith("/postal-id")


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
