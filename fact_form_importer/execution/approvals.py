"""Atomic mutable approval state for immutable LLM review artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from fact_form_importer.execution.atomic_state import atomic_write_json, file_lock
from fact_form_importer.execution.models import utc_now


APPROVAL_LEDGER_VERSION = "1.6"
LEGACY_ADDRESS_AUTO_APPROVAL_POLICY_VERSION = "high-single-os-candidate-v1"
ADDRESS_AUTO_APPROVAL_POLICY_VERSION = "high-supplied-os-candidate-v2"
ADDRESS_AUTO_APPROVAL_POLICY_VERSIONS = {
    LEGACY_ADDRESS_AUTO_APPROVAL_POLICY_VERSION,
    ADDRESS_AUTO_APPROVAL_POLICY_VERSION,
}
ADDRESS_AUTO_APPROVAL_RATIONALE = (
    "High-confidence address selected a supplied OS candidate without requesting review"
)
LEGACY_UNCHANGED_FIELD_AUTO_APPROVAL_POLICY_VERSION = "high-unchanged-field-v1"
FIELD_AUTO_APPROVAL_POLICY_VERSION = "high-accepted-field-v2"
FIELD_AUTO_APPROVAL_POLICY_VERSIONS = {
    LEGACY_UNCHANGED_FIELD_AUTO_APPROVAL_POLICY_VERSION,
    FIELD_AUTO_APPROVAL_POLICY_VERSION,
}
FIELD_AUTO_APPROVAL_RATIONALE = (
    "High-confidence accepted field result did not request human review"
)


class ApprovalDecision(BaseModel):
    recorded_at: str = Field(default_factory=utc_now)
    approval_method: Literal["manual", "policy"]
    policy_version: Optional[str] = None
    rationale: Optional[str] = None
    approved_value_hash: Optional[str] = None
    selected_uprn: Optional[str] = None


class LlmApproval(BaseModel):
    review_id: str
    approved_at: str = Field(default_factory=utc_now)
    approval_method: Literal["manual", "policy"] = "manual"
    policy_version: Optional[str] = None
    rationale: Optional[str] = None
    approved_address_patch: Optional[dict[str, Optional[str]]] = None
    selected_uprn: Optional[str] = None
    approved_value_hash: Optional[str] = None
    approved_field_value: Optional[str] = None
    field_value_overridden: bool = False
    omitted: bool = False
    decision_history: list[ApprovalDecision] = Field(default_factory=list)


class LlmDenial(BaseModel):
    review_id: str
    denied_at: str = Field(default_factory=utc_now)
    rationale: str = "Reviewer chose not to use this model-derived value"


class LlmApprovalLedger(BaseModel):
    ledger_version: str = APPROVAL_LEDGER_VERSION
    run_id: str
    updated_at: str = Field(default_factory=utc_now)
    approvals: dict[str, LlmApproval] = Field(default_factory=dict)
    denials: dict[str, LlmDenial] = Field(default_factory=dict)


class LlmApprovalStore:
    def __init__(self, output_root: Path) -> None:
        self.directory = output_root / "llm-approval-state"
        self._lock = Lock()

    def path_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def load(self, run_id: str) -> LlmApprovalLedger:
        with file_lock(self.path_for(run_id)):
            return self._load_unlocked(run_id)

    def _load_unlocked(self, run_id: str) -> LlmApprovalLedger:
        path = self.path_for(run_id)
        if not path.exists():
            return LlmApprovalLedger(run_id=run_id)
        return LlmApprovalLedger.model_validate_json(path.read_text(encoding="utf-8"))

    def approve(self, run_id: str, review_id: str) -> LlmApprovalLedger:
        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            changed = ledger.denials.pop(review_id, None) is not None
            if review_id not in ledger.approvals:
                ledger.approvals[review_id] = LlmApproval(review_id=review_id)
                changed = True
            if changed:
                ledger.ledger_version = APPROVAL_LEDGER_VERSION
                self._save(ledger)
            return ledger

    def approve_many(
        self, run_id: str, review_ids: set[str]
    ) -> tuple[LlmApprovalLedger, int]:
        """Atomically approve pending IDs while preserving explicit denials."""

        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            added = 0
            for review_id in sorted(review_ids):
                if review_id in ledger.approvals or review_id in ledger.denials:
                    continue
                ledger.approvals[review_id] = LlmApproval(review_id=review_id)
                added += 1
            if added:
                ledger.ledger_version = APPROVAL_LEDGER_VERSION
                self._save(ledger)
            return ledger, added

    def apply_test_approvals(
        self, run_id: str, decisions: dict[str, LlmApproval]
    ) -> tuple[LlmApprovalLedger, int]:
        """Atomically record deterministic testing decisions for pending IDs."""

        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            added = 0
            for review_id in sorted(decisions):
                if review_id in ledger.approvals or review_id in ledger.denials:
                    continue
                ledger.approvals[review_id] = decisions[review_id]
                added += 1
            if added:
                ledger.ledger_version = APPROVAL_LEDGER_VERSION
                self._save(ledger)
            return ledger, added

    def deny(self, run_id: str, review_id: str, rationale: str) -> LlmApprovalLedger:
        """Record a final human decision not to use a pending LLM result."""

        rationale = rationale.strip()
        if not rationale:
            raise ValueError("Enter a reason for denying this result")
        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            existing = ledger.denials.get(review_id)
            if existing is None or existing.rationale != rationale:
                ledger.denials[review_id] = LlmDenial(
                    review_id=review_id, rationale=rationale
                )
                ledger.ledger_version = APPROVAL_LEDGER_VERSION
                self._save(ledger)
            return ledger

    def approve_field(
        self,
        run_id: str,
        review_id: str,
        value: str | None,
        *,
        omitted: bool = False,
        rationale: str | None = None,
    ) -> LlmApprovalLedger:
        """Approve the exact text (or omission) used for an optional reviewed field."""

        if omitted:
            value = None
            rationale = (rationale or "").strip()
            if not rationale:
                raise ValueError("Enter a reason for omitting this optional value")
        else:
            value = (value or "").strip()
            if not value:
                raise ValueError("Enter the field value to approve")
            if len(value) > 250:
                raise ValueError("Contact explanations must be 250 characters or fewer")
            rationale = "Reviewer approved the displayed field text"
        value_hash = _canonical_hash({"value": value, "omitted": omitted})
        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            existing = ledger.approvals.get(review_id)
            if (
                existing
                and existing.approved_value_hash == value_hash
                and existing.rationale == rationale
            ):
                return ledger
            ledger.approvals[review_id] = LlmApproval(
                review_id=review_id,
                approval_method="manual",
                rationale=rationale,
                approved_value_hash=value_hash,
                approved_field_value=value,
                field_value_overridden=True,
                omitted=omitted,
            )
            ledger.denials.pop(review_id, None)
            ledger.ledger_version = APPROVAL_LEDGER_VERSION
            self._save(ledger)
            return ledger

    def reconsider(self, run_id: str, review_id: str) -> tuple[LlmApprovalLedger, bool]:
        """Return a denied result to the pending queue."""

        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            removed = ledger.denials.pop(review_id, None) is not None
            if removed:
                ledger.ledger_version = APPROVAL_LEDGER_VERSION
                self._save(ledger)
            return ledger, removed

    def approve_address(
        self,
        run_id: str,
        review_id: str,
        address_patch: dict[str, Optional[str]],
        *,
        selected_uprn: str | None = None,
        rationale: str | None = None,
    ) -> LlmApprovalLedger:
        """Record the exact reviewer-approved address while retaining decision history."""

        selected_uprn = (selected_uprn or "").strip() or None
        rationale = (rationale or "").strip() or None
        if selected_uprn and not rationale:
            raise ValueError("Enter a reason for selecting this OS address candidate")
        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            existing = ledger.approvals.get(review_id)
            effective_uprn = selected_uprn or (existing.selected_uprn if existing else None)
            value_hash = _canonical_hash(
                {"address_patch": address_patch, "selected_uprn": effective_uprn}
            )
            if (
                existing
                and existing.approval_method == "manual"
                and existing.approved_value_hash == value_hash
                and (not rationale or existing.rationale == rationale)
            ):
                return ledger
            history = list(existing.decision_history) if existing else []
            if existing:
                history.append(
                    ApprovalDecision(
                        recorded_at=existing.approved_at,
                        approval_method=existing.approval_method,
                        policy_version=existing.policy_version,
                        rationale=existing.rationale,
                        approved_value_hash=existing.approved_value_hash,
                        selected_uprn=existing.selected_uprn,
                    )
                )
            ledger.approvals[review_id] = LlmApproval(
                review_id=review_id,
                approval_method="manual",
                rationale=rationale or "Reviewer approved the displayed address text",
                approved_address_patch=address_patch,
                approved_value_hash=value_hash,
                selected_uprn=effective_uprn,
                decision_history=history,
            )
            ledger.denials.pop(review_id, None)
            ledger.ledger_version = APPROVAL_LEDGER_VERSION
            self._save(ledger)
            return ledger

    def reconcile_policies(
        self,
        run_id: str,
        review_report: dict[str, Any],
        readiness_report: dict[str, Any],
    ) -> tuple[LlmApprovalLedger, int]:
        """Atomically add every missing approval selected by versioned policies."""

        eligible = {
            review_id: (
                ADDRESS_AUTO_APPROVAL_POLICY_VERSION,
                ADDRESS_AUTO_APPROVAL_RATIONALE,
            )
            for review_id in policy_eligible_address_review_ids(
                review_report, readiness_report
            )
        }
        eligible.update(
            {
                review_id: (
                    FIELD_AUTO_APPROVAL_POLICY_VERSION,
                    FIELD_AUTO_APPROVAL_RATIONALE,
                )
                for review_id in policy_eligible_high_confidence_field_review_ids(
                    review_report
                )
            }
        )
        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            added = 0
            for review_id in sorted(eligible):
                if review_id in ledger.approvals or review_id in ledger.denials:
                    continue
                policy_version, rationale = eligible[review_id]
                ledger.approvals[review_id] = LlmApproval(
                    review_id=review_id,
                    approval_method="policy",
                    policy_version=policy_version,
                    rationale=rationale,
                )
                added += 1
            if added:
                ledger.ledger_version = APPROVAL_LEDGER_VERSION
                self._save(ledger)
            return ledger, added

    def reconcile_address_policy(
        self,
        run_id: str,
        review_report: dict[str, Any],
        readiness_report: dict[str, Any],
    ) -> tuple[LlmApprovalLedger, int]:
        """Backward-compatible name for callers created before field policies."""

        return self.reconcile_policies(run_id, review_report, readiness_report)

    def _save(self, ledger: LlmApprovalLedger) -> None:
        ledger.updated_at = utc_now()
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(ledger.run_id)
        atomic_write_json(path, ledger.model_dump(mode="json"))


def policy_eligible_address_review_ids(
    review_report: dict[str, Any], readiness_report: dict[str, Any]
) -> set[str]:
    """Return actionable high-confidence selections of supplied OS candidates."""

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


def policy_eligible_high_confidence_field_review_ids(
    review_report: dict[str, Any],
) -> set[str]:
    """Return accepted high-confidence field results safe for policy approval."""

    eligible: set[str] = set()
    for item in review_report.get("items", []):
        result = item.get("model_result")
        if (
            item.get("kind") != "field"
            or item.get("outcome") != "accepted"
            or not item.get("review_id")
            or not isinstance(result, dict)
            or result.get("operation") not in {"set", "clear"}
            or result.get("confidence") != "high"
            or result.get("needs_human_review") is not False
            or _requires_manual_overlength_review(item)
        ):
            continue
        eligible.add(str(item["review_id"]))
    return eligible


def _requires_manual_overlength_review(item: dict[str, Any]) -> bool:
    if not str(item.get("field") or "").endswith(".explanation"):
        return False
    llm_input = item.get("llm_input")
    if not isinstance(llm_input, dict):
        return False
    submitted = llm_input.get("cleaned_value")
    return isinstance(submitted, str) and len(submitted) > 250


def policy_eligible_unchanged_field_review_ids(
    review_report: dict[str, Any],
) -> set[str]:
    """Compatibility alias for callers using the former policy helper name."""

    return policy_eligible_high_confidence_field_review_ids(review_report)


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
    ):
        return False
    return any(
        isinstance(candidate, dict)
        and str(candidate.get("uprn") or "") == selected_uprn
        for candidate in candidates
    )


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
