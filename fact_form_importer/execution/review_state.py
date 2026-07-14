"""Mutable review decisions derived from immutable API section plans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from pydantic import BaseModel, Field

from fact_form_importer.execution.models import utc_now


EXECUTION_REVIEW_LEDGER_VERSION = "1.0"


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def target_change_id(court_slug: str, action_id: str) -> str:
    digest = hashlib.sha256(f"{court_slug}|{action_id}".encode("utf-8")).hexdigest()[:16]
    return f"target-{digest}"


class SourceSelection(BaseModel):
    court_slug: str
    source_row_number: int
    selected_at: str = Field(default_factory=utc_now)


class TargetComparison(BaseModel):
    change_id: str
    court_slug: str
    action_id: str
    resource: str
    source_row_number: Optional[int] = None
    captured_at: str = Field(default_factory=utc_now)
    current: Any
    proposed: Any
    current_hash: str
    proposed_hash: str
    operations: list[dict[str, Any]] = Field(default_factory=list)
    has_existing_data: bool = False
    is_no_change: bool = False


class TargetApproval(BaseModel):
    change_id: str
    current_hash: str
    proposed_hash: str
    approved_at: str = Field(default_factory=utc_now)


class ExecutionReviewLedger(BaseModel):
    ledger_version: str = EXECUTION_REVIEW_LEDGER_VERSION
    run_id: str
    updated_at: str = Field(default_factory=utc_now)
    source_selections: dict[str, SourceSelection] = Field(default_factory=dict)
    comparisons: dict[str, TargetComparison] = Field(default_factory=dict)
    target_approvals: dict[str, TargetApproval] = Field(default_factory=dict)


class ExecutionReviewStore:
    def __init__(self, output_root: Path) -> None:
        self.directory = output_root / "execution-review-state"
        self._lock = RLock()

    def path_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def load(self, run_id: str) -> ExecutionReviewLedger:
        path = self.path_for(run_id)
        if not path.exists():
            return ExecutionReviewLedger(run_id=run_id)
        return ExecutionReviewLedger.model_validate_json(path.read_text(encoding="utf-8"))

    def select_source(
        self, run_id: str, court_slug: str, source_row_number: int
    ) -> ExecutionReviewLedger:
        with self._lock:
            ledger = self.load(run_id)
            existing = ledger.source_selections.get(court_slug)
            if existing and existing.source_row_number == source_row_number:
                return ledger
            ledger.source_selections[court_slug] = SourceSelection(
                court_slug=court_slug, source_row_number=source_row_number
            )
            for change_id, comparison in list(ledger.comparisons.items()):
                if comparison.court_slug == court_slug:
                    ledger.comparisons.pop(change_id, None)
                    ledger.target_approvals.pop(change_id, None)
            return self.save(ledger)

    def save_comparison(
        self, run_id: str, comparison: TargetComparison
    ) -> ExecutionReviewLedger:
        with self._lock:
            ledger = self.load(run_id)
            previous = ledger.comparisons.get(comparison.change_id)
            ledger.comparisons[comparison.change_id] = comparison
            if previous and (
                previous.current_hash != comparison.current_hash
                or previous.proposed_hash != comparison.proposed_hash
            ):
                ledger.target_approvals.pop(comparison.change_id, None)
            return self.save(ledger)

    def approve_target(self, run_id: str, change_id: str) -> ExecutionReviewLedger:
        with self._lock:
            ledger = self.load(run_id)
            comparison = ledger.comparisons.get(change_id)
            if comparison is None:
                raise ValueError("Refresh the live FaCT comparison before approving this change")
            if not comparison.has_existing_data or comparison.is_no_change:
                raise ValueError("This section does not require an overwrite approval")
            approval = ledger.target_approvals.get(change_id)
            if approval and (
                approval.current_hash == comparison.current_hash
                and approval.proposed_hash == comparison.proposed_hash
            ):
                return ledger
            ledger.target_approvals[change_id] = TargetApproval(
                change_id=change_id,
                current_hash=comparison.current_hash,
                proposed_hash=comparison.proposed_hash,
            )
            return self.save(ledger)

    def invalidate_actions(
        self, run_id: str, action_ids: set[str]
    ) -> ExecutionReviewLedger:
        """Discard comparisons and approvals whose proposal has been edited."""

        with self._lock:
            ledger = self.load(run_id)
            changed = False
            for change_id, comparison in list(ledger.comparisons.items()):
                if comparison.action_id not in action_ids:
                    continue
                ledger.comparisons.pop(change_id, None)
                ledger.target_approvals.pop(change_id, None)
                changed = True
            return self.save(ledger) if changed else ledger

    def save(self, ledger: ExecutionReviewLedger) -> ExecutionReviewLedger:
        ledger.updated_at = utc_now()
        ledger.ledger_version = EXECUTION_REVIEW_LEDGER_VERSION
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(ledger.run_id)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(ledger.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return ledger


def build_target_comparison(
    court_slug: str,
    action: dict[str, Any],
    current: Any,
) -> TargetComparison:
    proposed_items = action.get("proposed_items")
    if action.get("resource") in {"address", "contact_detail", "court_opening_hours"}:
        proposed: Any = proposed_items if isinstance(proposed_items, list) else [action.get("body", {})]
        current_value = current if isinstance(current, list) else []
    else:
        proposed = action.get("body", {})
        current_value = current if isinstance(current, dict) else {}
    operations = replacement_operations(action, current_value, proposed)
    return TargetComparison(
        change_id=target_change_id(court_slug, str(action["action_id"])),
        court_slug=court_slug,
        action_id=str(action["action_id"]),
        resource=str(action.get("resource") or ""),
        source_row_number=action.get("source_row_number"),
        current=current_value,
        proposed=proposed,
        current_hash=canonical_hash(current_value),
        proposed_hash=canonical_hash(proposed),
        operations=operations,
        has_existing_data=bool(current_value),
        is_no_change=_without_server_ids(current_value) == _without_server_ids(proposed),
    )


def replacement_operations(
    action: dict[str, Any], current: Any, proposed: Any
) -> list[dict[str, Any]]:
    resource = str(action.get("resource") or "")
    path = str(action.get("path") or "")
    if resource not in {"address", "contact_detail", "court_opening_hours"}:
        return [
            {
                "method": str(action.get("method") or "POST"),
                "path": path,
                "body": proposed,
                "purpose": "update" if current else "create",
            }
        ]

    current_items = list(current) if isinstance(current, list) else []
    proposed_items = list(proposed) if isinstance(proposed, list) else []
    current_groups = _group_by_business_key(resource, current_items)
    proposed_groups = _group_by_business_key(resource, proposed_items)
    operations: list[dict[str, Any]] = []
    deletions: list[dict[str, Any]] = []
    for key in sorted(set(current_groups) | set(proposed_groups)):
        existing = sorted(current_groups.get(key, []), key=lambda item: str(item.get("id") or ""))
        wanted = proposed_groups.get(key, [])
        paired = min(len(existing), len(wanted))
        for index in range(paired):
            item_id = existing[index].get("id")
            update_path = path
            if resource in {"address", "contact_detail"} and item_id:
                update_path = f"{path}/{item_id}"
            operations.append(
                {"method": "PUT", "path": update_path, "body": wanted[index], "purpose": "update"}
            )
        for item in wanted[paired:]:
            operations.append(
                {
                    "method": str(action.get("method") or "POST"),
                    "path": path,
                    "body": item,
                    "purpose": "create",
                }
            )
        for item in existing[paired:]:
            if item.get("id"):
                deletions.append(
                    {
                        "method": "DELETE",
                        "path": f"{path}/{item['id']}",
                        "body": {},
                        "purpose": "delete_surplus",
                    }
                )
    return operations + deletions


def _group_by_business_key(
    resource: str, items: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    keys = {
        "address": ("addressType",),
        "contact_detail": ("courtContactDescriptionId", "courtContactDescription"),
        "court_opening_hours": ("openingHourTypeId", "openingHourType"),
    }[resource]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, item in enumerate(items):
        value: Any = None
        for key in keys:
            value = item.get(key)
            if value is not None:
                break
        if isinstance(value, dict):
            value = value.get("id") or value.get("name")
        business_key = str(value) if value is not None else f"untyped-{index}"
        grouped.setdefault(business_key, []).append(item)
    return grouped


def _without_server_ids(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_server_ids(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _without_server_ids(item)
            for key, item in value.items()
            if key not in {"id", "createdAt", "updatedAt"}
        }
    return value
