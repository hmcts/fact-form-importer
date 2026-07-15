import json

from fact_form_importer.execution.approvals import (
    ADDRESS_AUTO_APPROVAL_POLICY_VERSION,
    ADDRESS_AUTO_APPROVAL_POLICY_VERSIONS,
    APPROVAL_LEDGER_VERSION,
    FIELD_AUTO_APPROVAL_POLICY_VERSION,
    LlmApprovalStore,
    policy_eligible_address_review_ids,
    policy_eligible_high_confidence_field_review_ids,
)


def test_address_policy_accepts_supplied_multi_candidate_high_result_only():
    eligible = _item("eligible")
    multiple = _item("multiple", candidates=[{"uprn": "uprn-1"}, {"uprn": "uprn-2"}])
    medium = _item("medium", confidence="medium")
    review = _item("review", needs_human_review=True)
    mismatched = _item("mismatched", candidates=[{"uprn": "different"}])
    blocked = _item("blocked", actionable=False)
    field = {**_item("field"), "kind": "field"}
    wrong_resource = {**_item("wrong-resource"), "dependent_action_ids": ["other-1"]}

    selected = policy_eligible_address_review_ids(
        {
            "items": [
                eligible,
                multiple,
                medium,
                review,
                mismatched,
                blocked,
                field,
                wrong_resource,
            ]
        },
        _readiness(),
    )

    assert selected == {"eligible", "multiple"}


def test_policy_reconciliation_is_atomic_idempotent_and_preserves_manual_approvals(tmp_path):
    store = LlmApprovalStore(tmp_path / "out")
    store.approve("run-1", "manual")
    report = {"items": [_item("automatic"), _item("manual")]}

    first, first_added = store.reconcile_address_policy("run-1", report, _readiness())
    first_timestamp = first.approvals["automatic"].approved_at
    second, second_added = store.reconcile_address_policy("run-1", report, _readiness())

    assert first_added == 1
    assert second_added == 0
    assert second.ledger_version == APPROVAL_LEDGER_VERSION
    assert second.approvals["manual"].approval_method == "manual"
    assert second.approvals["automatic"].approval_method == "policy"
    assert second.approvals["automatic"].policy_version == ADDRESS_AUTO_APPROVAL_POLICY_VERSION
    assert second.approvals["automatic"].approved_at == first_timestamp
    assert ADDRESS_AUTO_APPROVAL_POLICY_VERSIONS == {
        "high-single-os-candidate-v1",
        "high-supplied-os-candidate-v2",
    }
    assert not store.path_for("run-1").with_suffix(".json.tmp").exists()


def test_legacy_approval_ledger_defaults_existing_entries_to_manual(tmp_path):
    store = LlmApprovalStore(tmp_path / "out")
    store.directory.mkdir(parents=True)
    store.path_for("legacy").write_text(
        json.dumps(
            {
                "ledger_version": "1.0",
                "run_id": "legacy",
                "updated_at": "2026-07-14T10:00:00Z",
                "approvals": {
                    "review-1": {
                        "review_id": "review-1",
                        "approved_at": "2026-07-14T10:00:00Z",
                    }
                },
            }
        )
    )

    ledger = store.load("legacy")

    assert ledger.ledger_version == "1.0"
    assert ledger.approvals["review-1"].approval_method == "manual"


def test_field_policy_accepts_changed_high_confidence_sets_and_clears():
    exact = _field_item("exact")
    changed = _field_item("changed", value="Different")
    format_only = _field_item("format", cleaned="Ground floor", value="ground floor")
    medium = _field_item("medium", confidence="medium")
    review = _field_item("review", needs_human_review=True)
    cleared = _field_item("cleared", operation="clear", value=None)
    unresolved = _field_item("unresolved", operation="unresolved", value=None)
    type_changed = _field_item("type", cleaned=True, value="True")
    blocked = _field_item("blocked", actionable=False)

    selected = policy_eligible_high_confidence_field_review_ids(
        {
            "items": [
                exact,
                changed,
                format_only,
                medium,
                review,
                cleared,
                unresolved,
                type_changed,
                blocked,
            ]
        }
    )

    assert selected == {"exact", "changed", "format", "cleared", "blocked"}


def test_field_policy_reconciliation_records_v2_provenance(tmp_path):
    store = LlmApprovalStore(tmp_path / "out")

    ledger, added = store.reconcile_policies(
        "run-1", {"items": [_field_item("changed", value="Different")]}, _readiness()
    )

    assert added == 1
    assert ledger.approvals["changed"].policy_version == FIELD_AUTO_APPROVAL_POLICY_VERSION


def test_address_override_records_hash_and_policy_decision_history(tmp_path):
    store = LlmApprovalStore(tmp_path / "out")
    store.reconcile_policies("run-1", {"items": [_item("address")]}, _readiness())

    ledger = store.approve_address(
        "run-1",
        "address",
        {
            "addressLine1": "Reviewed Court",
            "addressLine2": None,
            "townCity": "London",
            "county": None,
            "postcode": "SW1A 1AA",
        },
    )

    approval = ledger.approvals["address"]
    assert approval.approval_method == "manual"
    assert approval.approved_address_patch["addressLine1"] == "Reviewed Court"
    assert approval.approved_value_hash
    assert approval.decision_history[0].approval_method == "policy"
    assert approval.decision_history[0].policy_version == ADDRESS_AUTO_APPROVAL_POLICY_VERSION


def _item(
    review_id,
    *,
    candidates=None,
    confidence="high",
    needs_human_review=False,
    actionable=True,
):
    return {
        "review_id": review_id,
        "kind": "address",
        "outcome": "accepted",
        "actionable": actionable,
        "dependent_action_ids": ["court-1"],
        "llm_input": {"candidates": candidates or [{"uprn": "uprn-1"}]},
        "model_result": {
            "uprn": "uprn-1",
            "confidence": confidence,
            "needs_human_review": needs_human_review,
        },
    }


def _field_item(
    review_id,
    *,
    cleaned="Available on the ground floor.",
    value="Available on the ground floor.",
    confidence="high",
    needs_human_review=False,
    operation="set",
    actionable=True,
):
    return {
        "review_id": review_id,
        "kind": "field",
        "outcome": "accepted",
        "actionable": actionable,
        "dependent_action_ids": ["court-1"] if actionable else [],
        "llm_input": {"cleaned_value": cleaned},
        "model_result": {
            "operation": operation,
            "value": value,
            "confidence": confidence,
            "needs_human_review": needs_human_review,
        },
    }


def _readiness():
    return {
        "records": [
            {
                "actions": [
                    {"action_id": "court-1", "resource": "address"},
                    {"action_id": "other-1", "resource": "contact_detail"},
                ]
            }
        ]
    }
