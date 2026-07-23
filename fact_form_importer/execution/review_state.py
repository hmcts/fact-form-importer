"""Mutable review decisions derived from immutable API section plans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from threading import RLock
from typing import Any, Optional
import uuid

from pydantic import BaseModel, Field

from fact_form_importer.execution.models import utc_now
from fact_form_importer.execution.atomic_state import (
    atomic_write_json,
    decode_first_json_object,
    file_lock,
)
from fact_form_importer.output.fact_api_manifest import MISSING_SUPPORT_PHONE_PLACEHOLDER


EXECUTION_REVIEW_LEDGER_VERSION = "1.3"
_REQUEST_FIELDS_BY_RESOURCE = {
    "building_facilities": {
        "courtId", "parking", "freeWaterDispensers", "snackVendingMachines",
        "drinkVendingMachines", "cafeteria", "waitingArea", "waitingAreaChildren",
        "quietRoom", "babyChanging", "wifi",
    },
    "accessibility_options": {
        "courtId", "accessibleParking", "accessibleParkingPhoneNumber",
        "accessibleToiletDescription", "accessibleEntrance",
        "accessibleEntrancePhoneNumber", "hearingEnhancementEquipment", "lift",
        "liftDoorWidth", "liftDoorLimit", "liftSupportPhoneNumber", "quietRoom",
    },
    "translation_services": {"courtId", "phoneNumber", "email"},
    "professional_information": {"professionalInformation"},
    "counter_service_opening_hours": {
        "courtId", "counterService", "assistWithForms", "assistWithDocuments",
        "assistWithSupport", "appointmentNeeded", "appointmentContact", "courtTypes",
        "openingTimesDetails",
    },
    "address": {
        "courtId", "addressLine1", "addressLine2", "townCity", "county", "postcode",
        "addressType", "areasOfLaw", "courtTypes",
    },
    "contact_detail": {
        "courtId", "courtContactDescriptionId", "explanation", "phoneNumber", "email",
    },
    "court_opening_hours": {"courtId", "openingHourTypeId", "openingTimesDetails"},
}


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


class CourtTargetOverride(BaseModel):
    source_row_number: int
    submitted_slug: str
    target_slug: str
    target_court_id: str
    target_court_name: Optional[str] = None
    selected_at: str = Field(default_factory=utc_now)


class TargetComparison(BaseModel):
    change_id: str
    court_slug: str
    action_id: str
    resource: str
    source_row_number: Optional[int] = None
    captured_at: str = Field(default_factory=utc_now)
    current: Any
    submitted: Any = None
    proposed: Any
    current_hash: str
    proposed_hash: str
    operations: list[dict[str, Any]] = Field(default_factory=list)
    has_existing_data: bool = False
    is_no_change: bool = False
    merge_conflicts: list[str] = Field(default_factory=list)


class TargetApproval(BaseModel):
    change_id: str
    current_hash: str
    proposed_hash: str
    approved_at: str = Field(default_factory=utc_now)


class CollectionItemResolution(BaseModel):
    item_id: str
    action_id: str
    resource: str
    decision: str
    rationale: str
    replacement_type_id: Optional[str] = None
    decided_at: str = Field(default_factory=utc_now)


class CourtDisposition(BaseModel):
    source_row_number: int
    court_slug: Optional[str] = None
    status: str = "unactionable"
    rationale: str
    decided_at: str = Field(default_factory=utc_now)


class ExecutionReviewLedger(BaseModel):
    ledger_version: str = EXECUTION_REVIEW_LEDGER_VERSION
    run_id: str
    updated_at: str = Field(default_factory=utc_now)
    source_selections: dict[str, SourceSelection] = Field(default_factory=dict)
    court_target_overrides: dict[str, CourtTargetOverride] = Field(default_factory=dict)
    comparisons: dict[str, TargetComparison] = Field(default_factory=dict)
    target_approvals: dict[str, TargetApproval] = Field(default_factory=dict)
    collection_item_resolutions: dict[str, CollectionItemResolution] = Field(
        default_factory=dict
    )
    court_dispositions: dict[str, CourtDisposition] = Field(default_factory=dict)
    plan_manifest_version: Optional[str] = None


class ExecutionReviewStore:
    def __init__(self, output_root: Path) -> None:
        self.directory = output_root / "execution-review-state"
        self._lock = RLock()

    def path_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def load(self, run_id: str) -> ExecutionReviewLedger:
        path = self.path_for(run_id)
        with self._lock, file_lock(path):
            return self._load_unlocked(run_id)

    def _load_unlocked(self, run_id: str) -> ExecutionReviewLedger:
        path = self.path_for(run_id)
        if not path.exists():
            return ExecutionReviewLedger(run_id=run_id)
        text = path.read_text(encoding="utf-8")
        try:
            return ExecutionReviewLedger.model_validate_json(text)
        except ValueError as original_error:
            backup = path.with_suffix(path.suffix + ".bak")
            if backup.exists():
                try:
                    return ExecutionReviewLedger.model_validate_json(
                        backup.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    pass
            try:
                prefix, suffix = decode_first_json_object(text)
                if not suffix:
                    raise original_error
                recovered = ExecutionReviewLedger.model_validate(prefix)
            except (TypeError, ValueError, json.JSONDecodeError):
                raise original_error
            diagnostic = path.with_suffix(
                path.suffix + f".corrupt-{uuid.uuid4().hex[:12]}"
            )
            shutil.copyfile(path, diagnostic)
            atomic_write_json(path, recovered.model_dump(mode="json"), backup=False)
            return recovered

    def select_source(
        self, run_id: str, court_slug: str, source_row_number: int
    ) -> ExecutionReviewLedger:
        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
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
            return self._save_unlocked(ledger)

    def set_court_target_override(
        self,
        run_id: str,
        override: CourtTargetOverride,
    ) -> ExecutionReviewLedger:
        """Persist a validated target court and invalidate row-bound comparisons."""

        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            key = str(override.source_row_number)
            existing = ledger.court_target_overrides.get(key)
            if existing and (
                existing.submitted_slug == override.submitted_slug
                and existing.target_slug == override.target_slug
                and existing.target_court_id == override.target_court_id
                and existing.target_court_name == override.target_court_name
            ):
                return ledger
            ledger.court_target_overrides[key] = override
            for change_id, comparison in list(ledger.comparisons.items()):
                if comparison.source_row_number != override.source_row_number:
                    continue
                ledger.comparisons.pop(change_id, None)
                ledger.target_approvals.pop(change_id, None)
            return self._save_unlocked(ledger)

    def save_comparison(
        self, run_id: str, comparison: TargetComparison
    ) -> ExecutionReviewLedger:
        return self.save_comparisons(run_id, [comparison])

    def save_comparisons(
        self, run_id: str, comparisons: list[TargetComparison]
    ) -> ExecutionReviewLedger:
        """Atomically persist a comparison scan without rewriting per section."""

        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            changed = False
            for comparison in comparisons:
                previous = ledger.comparisons.get(comparison.change_id)
                if previous and _comparison_content(previous) == _comparison_content(
                    comparison
                ):
                    continue
                ledger.comparisons[comparison.change_id] = comparison
                changed = True
                if previous and (
                    previous.current_hash != comparison.current_hash
                    or previous.proposed_hash != comparison.proposed_hash
                ):
                    ledger.target_approvals.pop(comparison.change_id, None)
            return self._save_unlocked(ledger) if changed else ledger

    def approve_target(self, run_id: str, change_id: str) -> ExecutionReviewLedger:
        ledger, _ = self.approve_targets(run_id, {change_id})
        return ledger

    def approve_targets(
        self, run_id: str, change_ids: set[str]
    ) -> tuple[ExecutionReviewLedger, int]:
        """Atomically approve a validated set of existing-data comparisons."""

        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            comparisons: list[TargetComparison] = []
            for change_id in sorted(change_ids):
                comparison = ledger.comparisons.get(change_id)
                if comparison is None:
                    raise ValueError(
                        "Refresh the live FaCT comparison before approving this change"
                    )
                if not comparison.has_existing_data or comparison.is_no_change:
                    raise ValueError(
                        "This section does not require an existing-data change approval"
                    )
                if comparison.merge_conflicts:
                    raise ValueError(
                        "Resolve ambiguous business-type matches before approving"
                    )
                comparisons.append(comparison)
            added = 0
            for comparison in comparisons:
                approval = ledger.target_approvals.get(comparison.change_id)
                if approval and (
                    approval.current_hash == comparison.current_hash
                    and approval.proposed_hash == comparison.proposed_hash
                ):
                    continue
                ledger.target_approvals[comparison.change_id] = TargetApproval(
                    change_id=comparison.change_id,
                    current_hash=comparison.current_hash,
                    proposed_hash=comparison.proposed_hash,
                )
                added += 1
            return (self._save_unlocked(ledger) if added else ledger), added

    def invalidate_actions(
        self, run_id: str, action_ids: set[str]
    ) -> ExecutionReviewLedger:
        """Discard comparisons and approvals whose proposal has been edited."""

        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            changed = False
            for change_id, comparison in list(ledger.comparisons.items()):
                if comparison.action_id not in action_ids:
                    continue
                ledger.comparisons.pop(change_id, None)
                ledger.target_approvals.pop(change_id, None)
                changed = True
            return self._save_unlocked(ledger) if changed else ledger

    def resolve_collection_item(
        self, run_id: str, resolution: CollectionItemResolution
    ) -> ExecutionReviewLedger:
        return self.resolve_collection_items(run_id, [resolution])

    def resolve_collection_items(
        self, run_id: str, resolutions: list[CollectionItemResolution]
    ) -> ExecutionReviewLedger:
        """Persist one or more item decisions in a single atomic ledger update."""

        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            action_ids = {resolution.action_id for resolution in resolutions}
            for resolution in resolutions:
                ledger.collection_item_resolutions[resolution.item_id] = resolution
            for change_id, comparison in list(ledger.comparisons.items()):
                if comparison.action_id not in action_ids:
                    continue
                ledger.comparisons.pop(change_id, None)
                ledger.target_approvals.pop(change_id, None)
            return self._save_unlocked(ledger)

    def close_court(
        self, run_id: str, disposition: CourtDisposition
    ) -> ExecutionReviewLedger:
        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            ledger.court_dispositions[str(disposition.source_row_number)] = disposition
            for change_id, comparison in list(ledger.comparisons.items()):
                if comparison.source_row_number != disposition.source_row_number:
                    continue
                ledger.comparisons.pop(change_id, None)
                ledger.target_approvals.pop(change_id, None)
            return self._save_unlocked(ledger)

    def reconcile_plan_version(
        self, run_id: str, manifest_version: str
    ) -> ExecutionReviewLedger:
        """Record the plan version; action-specific edits perform invalidation."""

        with self._lock, file_lock(self.path_for(run_id)):
            ledger = self._load_unlocked(run_id)
            if ledger.plan_manifest_version == manifest_version:
                return ledger
            ledger.plan_manifest_version = manifest_version
            return self._save_unlocked(ledger)

    def save(self, ledger: ExecutionReviewLedger) -> ExecutionReviewLedger:
        with self._lock, file_lock(self.path_for(ledger.run_id)):
            return self._save_unlocked(ledger)

    def _save_unlocked(self, ledger: ExecutionReviewLedger) -> ExecutionReviewLedger:
        ledger.updated_at = utc_now()
        ledger.ledger_version = EXECUTION_REVIEW_LEDGER_VERSION
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(ledger.run_id)
        atomic_write_json(path, ledger.model_dump(mode="json"))
        return ledger


def _comparison_content(comparison: TargetComparison) -> dict[str, Any]:
    payload = comparison.model_dump(mode="json")
    payload.pop("captured_at", None)
    return payload


def build_target_comparison(
    court_slug: str,
    action: dict[str, Any],
    current: Any,
) -> TargetComparison:
    proposed_items = action.get("proposed_items")
    if action.get("resource") in {"address", "contact_detail", "court_opening_hours"}:
        submitted: Any = (
            proposed_items if isinstance(proposed_items, list) else [action.get("body", {})]
        )
        current_value = current if isinstance(current, list) else []
    else:
        submitted = action.get("body", {})
        current_value = current if isinstance(current, dict) else {}
    proposed, operations, conflicts = merged_target_state(action, current_value, submitted)
    return TargetComparison(
        change_id=target_change_id(court_slug, str(action["action_id"])),
        court_slug=court_slug,
        action_id=str(action["action_id"]),
        resource=str(action.get("resource") or ""),
        source_row_number=action.get("source_row_number"),
        current=current_value,
        submitted=submitted,
        proposed=proposed,
        current_hash=canonical_hash(current_value),
        proposed_hash=canonical_hash(proposed),
        operations=operations,
        has_existing_data=bool(current_value),
        is_no_change=_without_server_ids(current_value) == _without_server_ids(proposed),
        merge_conflicts=conflicts,
    )


def replacement_operations(
    action: dict[str, Any], current: Any, proposed: Any
) -> list[dict[str, Any]]:
    """Backward-compatible operation helper using the merged update policy."""

    return merged_target_state(action, current, proposed)[1]


def merged_target_state(
    action: dict[str, Any], current: Any, submitted: Any
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    resource = str(action.get("resource") or "")
    path = str(action.get("path") or "")
    if resource not in {"address", "contact_detail", "court_opening_hours"}:
        current_value = dict(current) if isinstance(current, dict) else {}
        submitted_value = dict(submitted) if isinstance(submitted, dict) else {}
        effective = _deep_merge(current_value, submitted_value)
        for field in action.get("clear_fields", []):
            _set_nested_value(effective, str(field), None)
        effective = _apply_request_defaults(resource, effective)
        return effective, [
            {
                "method": str(action.get("method") or "POST"),
                "path": path,
                "body": _operation_body(resource, effective),
                "purpose": "update" if current_value else "create",
            }
        ], []

    current_items = list(current) if isinstance(current, list) else []
    proposed_items = list(submitted) if isinstance(submitted, list) else []
    source_clear_fields = action.get("proposed_item_clear_fields") or []
    proposed_items, clear_fields = _coalesce_proposed_items(
        resource, proposed_items, source_clear_fields
    )
    current_groups = _group_by_business_key(resource, current_items)
    proposed_groups = _group_by_business_key(resource, proposed_items)
    operations: list[dict[str, Any]] = []
    conflicts: list[str] = []
    effective = [dict(item) for item in current_items]
    current_indexes = _group_indexes_by_business_key(resource, current_items)
    proposed_indexes = _group_indexes_by_business_key(resource, proposed_items)
    for key in sorted(proposed_groups):
        existing = current_groups.get(key, [])
        wanted = proposed_groups[key]
        if len(existing) > 1 or len(wanted) > 1:
            conflicts.append(
                f"Multiple {resource.replace('_', ' ')} entries use business type '{key}'"
            )
            continue
        proposed_index = proposed_indexes[key][0]
        item = dict(wanted[0])
        item_clear_fields = clear_fields[proposed_index] if proposed_index < len(clear_fields) else []
        if existing:
            current_index = current_indexes[key][0]
            merged = _deep_merge(existing[0], item)
            for field in item_clear_fields:
                _set_nested_value(merged, str(field), None)
            effective[current_index] = merged
            item_id = existing[0].get("id")
            update_path = path
            if resource in {"address", "contact_detail"} and item_id:
                update_path = f"{path}/{item_id}"
            operations.append(
                {
                    "method": "PUT",
                    "path": update_path,
                    "body": _operation_body(resource, merged),
                    "purpose": "update",
                }
            )
        else:
            for field in item_clear_fields:
                _set_nested_value(item, str(field), None)
            effective.append(item)
            operations.append(
                {
                    "method": str(action.get("method") or "POST"),
                    "path": path,
                    "body": _operation_body(resource, item),
                    "purpose": "create",
                }
            )
    return effective, operations, conflicts


def _coalesce_proposed_items(
    resource: str,
    items: list[dict[str, Any]],
    clear_fields: list[list[str]],
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """Collapse exact duplicates and non-conflicting complementary contacts."""

    groups = _group_indexes_by_business_key(resource, items)
    coalesced: list[dict[str, Any]] = []
    coalesced_clears: list[list[str]] = []
    for indexes in groups.values():
        candidates = [dict(items[index]) for index in indexes]
        combined_clears = sorted(
            {
                str(field)
                for index in indexes
                for field in (clear_fields[index] if index < len(clear_fields) else [])
            }
        )
        canonical = {
            canonical_hash(_comparable_collection_item(candidate))
            for candidate in candidates
        }
        if len(canonical) == 1:
            coalesced.append(candidates[0])
            coalesced_clears.append(combined_clears)
            continue
        if resource == "contact_detail":
            merged = _merge_complementary_contacts(candidates)
            if merged is not None:
                coalesced.append(merged)
                coalesced_clears.append(combined_clears)
                continue
        coalesced.extend(candidates)
        for index in indexes:
            coalesced_clears.append(
                list(clear_fields[index]) if index < len(clear_fields) else []
            )
    return coalesced, coalesced_clears


def _comparable_collection_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key not in {"id", "court", "courtId", "createdAt", "updatedAt"}
    }


def _merge_complementary_contacts(
    contacts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    for contact in contacts:
        for field, value in contact.items():
            if value is None or value == "":
                continue
            if field not in merged or merged[field] is None or merged[field] == "":
                merged[field] = value
                continue
            if merged[field] != value:
                return None
    return merged


def _deep_merge(current: dict[str, Any], submitted: dict[str, Any]) -> dict[str, Any]:
    """Overlay submitted leaf values while preserving unsubmitted nested live data."""

    merged = dict(current)
    for field, submitted_value in submitted.items():
        current_value = merged.get(field)
        if isinstance(current_value, dict) and isinstance(submitted_value, dict):
            merged[field] = _deep_merge(current_value, submitted_value)
        else:
            merged[field] = submitted_value
    return merged


def _set_nested_value(value: dict[str, Any], path: str, replacement: Any) -> None:
    """Apply an explicit clear to a dotted field path."""

    parts = path.split(".")
    target = value
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = replacement


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


def _group_indexes_by_business_key(
    resource: str, items: list[dict[str, Any]]
) -> dict[str, list[int]]:
    grouped_items = _group_by_business_key(resource, items)
    indexes: dict[str, list[int]] = {key: [] for key in grouped_items}
    for index, item in enumerate(items):
        for key, candidates in grouped_items.items():
            if any(candidate is item for candidate in candidates):
                indexes[key].append(index)
                break
    return indexes


def _operation_body(resource: str, value: dict[str, Any]) -> dict[str, Any]:
    allowed = _REQUEST_FIELDS_BY_RESOURCE.get(resource)
    return {
        key: item
        for key, item in value.items()
        if key not in {"id", "court"} and (allowed is None or key in allowed)
    }


def _apply_request_defaults(resource: str, value: dict[str, Any]) -> dict[str, Any]:
    effective = dict(value)
    if resource != "accessibility_options":
        return effective
    if effective.get("accessibleEntrance") is False and not effective.get(
        "accessibleEntrancePhoneNumber"
    ):
        effective["accessibleEntrancePhoneNumber"] = MISSING_SUPPORT_PHONE_PLACEHOLDER
    if effective.get("lift") is False and not effective.get("liftSupportPhoneNumber"):
        effective["liftSupportPhoneNumber"] = MISSING_SUPPORT_PHONE_PLACEHOLDER
    return effective


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
