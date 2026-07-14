"""Atomic mutable approval state for immutable LLM review artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from fact_form_importer.execution.models import utc_now


APPROVAL_LEDGER_VERSION = "1.1"
ADDRESS_AUTO_APPROVAL_POLICY_VERSION = "high-single-os-candidate-v1"
ADDRESS_AUTO_APPROVAL_RATIONALE = (
    "High-confidence address selected the sole supplied OS candidate without requesting review"
)


class LlmApproval(BaseModel):
    review_id: str
    approved_at: str = Field(default_factory=utc_now)
    approval_method: Literal["manual", "policy"] = "manual"
    policy_version: Optional[str] = None
    rationale: Optional[str] = None


class LlmApprovalLedger(BaseModel):
    ledger_version: str = APPROVAL_LEDGER_VERSION
    run_id: str
    updated_at: str = Field(default_factory=utc_now)
    approvals: dict[str, LlmApproval] = Field(default_factory=dict)


class LlmApprovalStore:
    def __init__(self, output_root: Path) -> None:
        self.directory = output_root / "llm-approval-state"
        self._lock = Lock()

    def path_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def load(self, run_id: str) -> LlmApprovalLedger:
        path = self.path_for(run_id)
        if not path.exists():
            return LlmApprovalLedger(run_id=run_id)
        return LlmApprovalLedger.model_validate_json(path.read_text(encoding="utf-8"))

    def approve(self, run_id: str, review_id: str) -> LlmApprovalLedger:
        with self._lock:
            ledger = self.load(run_id)
            if review_id not in ledger.approvals:
                ledger.approvals[review_id] = LlmApproval(review_id=review_id)
                ledger.ledger_version = APPROVAL_LEDGER_VERSION
                self._save(ledger)
            return ledger

    def reconcile_address_policy(
        self,
        run_id: str,
        review_report: dict[str, Any],
        readiness_report: dict[str, Any],
    ) -> tuple[LlmApprovalLedger, int]:
        """Atomically add missing approvals selected by the versioned address policy."""

        eligible_ids = policy_eligible_address_review_ids(review_report, readiness_report)
        with self._lock:
            ledger = self.load(run_id)
            added = 0
            for review_id in sorted(eligible_ids):
                if review_id in ledger.approvals:
                    continue
                ledger.approvals[review_id] = LlmApproval(
                    review_id=review_id,
                    approval_method="policy",
                    policy_version=ADDRESS_AUTO_APPROVAL_POLICY_VERSION,
                    rationale=ADDRESS_AUTO_APPROVAL_RATIONALE,
                )
                added += 1
            if added:
                ledger.ledger_version = APPROVAL_LEDGER_VERSION
                self._save(ledger)
            return ledger, added

    def _save(self, ledger: LlmApprovalLedger) -> None:
        ledger.updated_at = utc_now()
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(ledger.run_id)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(ledger.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)


def policy_eligible_address_review_ids(
    review_report: dict[str, Any], readiness_report: dict[str, Any]
) -> set[str]:
    """Return actionable address review IDs that satisfy the strict automatic policy."""

    address_action_ids = {
        str(action["action_id"])
        for record in readiness_report.get("records", [])
        for action in record.get("actions", [])
        if action.get("resource") == "address" and action.get("action_id")
    }
    return {
        str(item["review_id"])
        for item in review_report.get("items", [])
        if _is_policy_eligible_address_item(item, address_action_ids)
    }


def _is_policy_eligible_address_item(item: dict[str, Any], address_action_ids: set[str]) -> bool:
    dependent_action_ids = {
        str(action_id) for action_id in item.get("dependent_action_ids", []) if action_id
    }
    if (
        item.get("kind") != "address"
        or item.get("outcome") != "accepted"
        or not item.get("actionable")
        or not dependent_action_ids & address_action_ids
        or not item.get("review_id")
    ):
        return False
    result = item.get("model_result")
    llm_input = item.get("llm_input")
    if not isinstance(result, dict) or not isinstance(llm_input, dict):
        return False
    selected_uprn = str(result.get("uprn") or "")
    candidates = llm_input.get("candidates")
    if (
        result.get("confidence") != "high"
        or result.get("needs_human_review") is not False
        or not selected_uprn
        or not isinstance(candidates, list)
        or len(candidates) != 1
        or not isinstance(candidates[0], dict)
    ):
        return False
    return str(candidates[0].get("uprn") or "") == selected_uprn
