import json

from fact_form_importer.execution.approvals import (
    ADDRESS_AUTO_APPROVAL_POLICY_VERSION,
    APPROVAL_LEDGER_VERSION,
    LlmApprovalStore,
    policy_eligible_address_review_ids,
)


def test_strict_address_policy_rejects_ambiguous_and_non_actionable_results():
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

    assert selected == {"eligible"}


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
