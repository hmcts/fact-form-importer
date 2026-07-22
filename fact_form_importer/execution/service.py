"""Conservative, one-court execution service for archived API action reports."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx

from fact_form_importer.config import AppConfig
from fact_form_importer.execution.approvals import LlmApprovalLedger, LlmApprovalStore
from fact_form_importer.execution.business_report import build_business_report
from fact_form_importer.execution.fact_api import ApiResponse, FactApiExecutionClient
from fact_form_importer.execution.ledger import ExecutionLedgerStore
from fact_form_importer.execution.models import (
    ActionAttempt,
    ActionExecutionState,
    CourtExecutionState,
    ExecutionLedger,
    utc_now,
)
from fact_form_importer.execution.overlay import derive_latest_execution_overlay
from fact_form_importer.execution.report import (
    EXECUTION_SUMMARY_VERSION,
    build_execution_summary,
)
from fact_form_importer.execution.review_state import (
    CollectionItemResolution,
    CourtDisposition,
    CourtTargetOverride,
    ExecutionReviewLedger,
    ExecutionReviewStore,
    TargetComparison,
    build_target_comparison,
    merged_target_state,
    target_change_id,
)
from fact_form_importer.llm.review import (
    filter_llm_actions_review,
    load_or_derive_llm_actions_review,
)
from fact_form_importer.models.court_submission import CourtSubmission
from fact_form_importer.output.duplicate_review import select_authoritative_submissions
from fact_form_importer.output.archive import load_run_archive
from fact_form_importer.output.fact_api_manifest import (
    API_MANIFEST_VERSION,
    is_unavailable_lift_measurement,
    normalise_fact_api_action_body,
    validate_fact_api_action_body,
)
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.validators.os_addresses import RateLimitedPostcodeLookup


@dataclass(frozen=True)
class AddressPreflightResult:
    status: Literal["ready", "blocked", "unknown"]
    http_status: int | None = None
    reason: str | None = None


def _unconfigured_postcode_lookup(_: str) -> ApiResponse:
    raise RuntimeError("No FaCT API client is available for address verification")


_ADDRESS_REVIEW_NOT_LOADED = object()


class ApiExecutionService:
    """Executes only actions from an immutable report after target preflight."""

    def __init__(
        self,
        output_root: Path,
        config: AppConfig | None = None,
        client: FactApiExecutionClient | None = None,
    ) -> None:
        self.output_root = output_root
        self.config = config or AppConfig()
        self.store = ExecutionLedgerStore(output_root)
        self.approval_store = LlmApprovalStore(output_root)
        self.review_store = ExecutionReviewStore(output_root)
        self._llm_review_cache: dict[str, dict[str, Any]] = {}
        self._submission_cache: dict[str, dict[int, dict[str, Any]]] = {}
        self._client = client
        self._postcode_lookup = RateLimitedPostcodeLookup(
            _unconfigured_postcode_lookup,
            min_interval_seconds=self.config.os_address_min_interval_seconds,
        )
        self._postcode_lock = Lock()

    def get_ledger(self, run_id: str) -> ExecutionLedger:
        return self.store.load(run_id)

    def get_readiness_report(self, run_id: str) -> dict[str, Any]:
        return self._readiness_report(run_id)

    def get_execution_review(self, run_id: str) -> ExecutionReviewLedger:
        self._readiness_report(run_id)
        return self.review_store.load(run_id)

    def select_source_row(
        self, run_id: str, court_slug: str, source_row_number: int
    ) -> ExecutionReviewLedger:
        record = self._record(run_id, court_slug)
        if source_row_number not in record.get("source_row_numbers", []):
            raise ValueError("The selected source row is not a duplicate candidate for this court")
        execution = self.store.load(run_id)
        court = execution.courts.get(court_slug)
        if court and any(action.status == "succeeded" for action in court.actions.values()):
            raise ValueError("The authoritative source row cannot change after execution succeeds")
        ledger = self.review_store.select_source(run_id, court_slug, source_row_number)
        self._save_execution_summary(run_id)
        return ledger

    def get_court_target_resolution(
        self, run_id: str, source_row_number: int
    ) -> dict[str, Any] | None:
        """Return immutable missing-slug evidence plus any mutable target choice."""

        submission = self._submissions_by_row(run_id).get(source_row_number)
        if submission is None:
            return None
        override = self.review_store.load(run_id).court_target_overrides.get(
            str(source_row_number)
        )
        return _court_target_resolution_payload(submission, override)

    def set_court_target_override(
        self, run_id: str, source_row_number: int, target_slug: str
    ) -> CourtTargetOverride:
        """Validate and persist a reviewer-selected existing FaCT court target."""

        target_slug = target_slug.strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", target_slug):
            raise ValueError(
                "Enter a lowercase FaCT court slug using letters, numbers and hyphens"
            )
        resolution = self.get_court_target_resolution(run_id, source_row_number)
        if resolution is None:
            raise ValueError("This source row does not have a missing FaCT court target")
        authoritative = self._authoritative_source_rows(run_id)
        if authoritative is not None and source_row_number not in authoritative:
            raise ValueError("A superseded source row cannot select an API court target")

        current_report = self._readiness_report(run_id)
        current_record = next(
            (
                record
                for record in current_report.get("records", [])
                if source_row_number in record.get("source_row_numbers", [])
            ),
            None,
        )
        execution = self.store.load(run_id)
        if current_record:
            current_state = execution.courts.get(
                str(current_record.get("court_slug") or "")
            )
            if current_state and any(
                action.status == "succeeded"
                for action in current_state.actions.values()
            ):
                raise ValueError("The target court cannot change after an API action succeeds")

        client, close = self._client_or_new()
        try:
            reference = client.lookup_court(target_slug)
        except httpx.HTTPStatusError as exc:
            _, reason = _preflight_error_details("court target lookup", exc)
            raise ValueError(reason) from exc
        finally:
            if close:
                client.close()
        if reference is None:
            raise ValueError(f"FaCT has no court with slug '{target_slug}'")

        canonical_slug = reference.slug
        conflicts = [
            record
            for record in current_report.get("records", [])
            if record.get("court_slug") == canonical_slug
            and source_row_number not in record.get("source_row_numbers", [])
        ]
        override_conflicts = [
            value
            for key, value in self.review_store.load(run_id).court_target_overrides.items()
            if key != str(source_row_number) and value.target_slug == canonical_slug
        ]
        if conflicts or override_conflicts:
            rows = sorted(
                {
                    int(row)
                    for record in conflicts
                    for row in record.get("source_row_numbers", [])
                    if isinstance(row, int)
                }
                | {value.source_row_number for value in override_conflicts}
            )
            row_text = ", ".join(str(row) for row in rows)
            raise ValueError(
                f"That FaCT court is already targeted by source row(s) {row_text}. "
                "Do not combine distinct submissions without resolving which form is authoritative."
            )

        override = CourtTargetOverride(
            source_row_number=source_row_number,
            submitted_slug=str(resolution["submitted_slug"]),
            target_slug=canonical_slug,
            target_court_id=reference.court_id,
            target_court_name=reference.name,
        )
        self.review_store.set_court_target_override(run_id, override)
        plan_path = self.output_root / "execution-review-state" / f"{run_id}.plan.json"
        if plan_path.exists():
            plan_path.unlink()
        self._readiness_report(run_id)
        self._save_execution_summary(run_id)
        return override

    def refresh_target_comparison(
        self, run_id: str, court_slug: str, action_id: str
    ) -> TargetComparison:
        record = self._record(run_id, court_slug)
        action = next(
            (item for item in record.get("actions", []) if item.get("action_id") == action_id),
            None,
        )
        if action is None:
            raise ValueError(f"Action '{action_id}' is not in court '{court_slug}'")
        self._require_selected_source(run_id, record, action)
        client, close = self._client_or_new()
        try:
            court = client.lookup_court(court_slug)
            if court is None:
                raise ValueError("Court does not exist in FaCT")
            effective_action = dict(action)
            effective_action["proposed_items"] = self._execution_items(
                run_id, action, court.court_id
            )
            if action.get("resource") not in {
                "address",
                "contact_detail",
                "court_opening_hours",
            }:
                effective_action["body"] = self._execution_body(
                    run_id, action, court.court_id
                )
            response = client.get(_preflight_path(str(action["path"])))
            if response.status_code in {401, 403}:
                raise ValueError(
                    f"FaCT API rejected the target section comparison "
                    f"(HTTP {response.status_code}). Refresh FACT_DATA_API_BEARER_TOKEN "
                    "and restart the importer UI."
                )
            if response.status_code not in {200, 204, 404}:
                raise ValueError(
                    f"Target section comparison returned HTTP {response.status_code}"
                )
            comparison = build_target_comparison(court_slug, effective_action, response.body)
            self.review_store.save_comparison(run_id, comparison)
            self._save_execution_summary(run_id)
            return comparison
        except httpx.HTTPStatusError as exc:
            _, reason = _preflight_error_details("court lookup", exc)
            raise ValueError(reason) from exc
        finally:
            if close:
                client.close()

    def refresh_all_target_comparisons(self, run_id: str) -> None:
        """Fetch all live sections, batching court lookup and sidecar persistence."""

        report = self._readiness_report(run_id)
        failures: list[str] = []
        comparisons: list[TargetComparison] = []
        client, close = self._client_or_new()
        review = self.review_store.load(run_id)
        try:
            for record in report.get("records", []):
                court_slug = str(record.get("court_slug") or "")
                try:
                    actions = self._active_actions(run_id, record)
                    if any(action.get("source_selection_required") for action in actions):
                        if review.source_selections.get(court_slug) is None:
                            continue
                    court = client.lookup_court(court_slug)
                    if court is None:
                        raise ValueError("Court does not exist in FaCT")
                    for action in actions:
                        comparisons.append(
                            self._target_comparison_with_client(
                                run_id,
                                court_slug,
                                action,
                                court.court_id,
                                client,
                            )
                        )
                except (ValueError, httpx.HTTPError) as exc:
                    _, reason = _preflight_error_details("court lookup", exc)
                    if "Refresh FACT_DATA_API_BEARER_TOKEN" in reason:
                        raise ValueError(reason) from exc
                    failures.append(f"{court_slug}: {type(exc).__name__}")
        finally:
            if close:
                client.close()
        if comparisons:
            self.review_store.save_comparisons(run_id, comparisons)
        self._save_execution_summary(run_id)
        if failures:
            raise ValueError(
                f"FaCT comparison scan completed with {len(failures)} court error(s)"
            )

    def _target_comparison_with_client(
        self,
        run_id: str,
        court_slug: str,
        action: dict[str, Any],
        court_id: str,
        client: FactApiExecutionClient,
    ) -> TargetComparison:
        effective_action = dict(action)
        effective_action["proposed_items"] = self._execution_items(
            run_id, action, court_id
        )
        if action.get("resource") not in {
            "address",
            "contact_detail",
            "court_opening_hours",
        }:
            effective_action["body"] = self._execution_body(run_id, action, court_id)
        response = client.get(_preflight_path(str(action["path"])))
        if response.status_code in {401, 403}:
            raise ValueError(
                f"FaCT API rejected the target section comparison "
                f"(HTTP {response.status_code}). Refresh FACT_DATA_API_BEARER_TOKEN "
                "and restart the importer UI."
            )
        if response.status_code not in {200, 204, 404}:
            raise ValueError(
                f"Target section comparison returned HTTP {response.status_code}"
            )
        return build_target_comparison(court_slug, effective_action, response.body)

    def approve_target_change(self, run_id: str, change_id: str) -> ExecutionReviewLedger:
        self._readiness_report(run_id)
        ledger = self.review_store.approve_target(run_id, change_id)
        self._save_execution_summary(run_id)
        return ledger

    def approve_all_target_changes(
        self, run_id: str
    ) -> tuple[ExecutionReviewLedger, int]:
        """Approve every checked, unambiguous existing-data diff without executing."""

        self._readiness_report(run_id)
        review = self.review_store.load(run_id)
        eligible = {
            change_id
            for change_id, comparison in review.comparisons.items()
            if comparison.has_existing_data
            and not comparison.is_no_change
            and not comparison.merge_conflicts
            and not (
                (approval := review.target_approvals.get(change_id))
                and approval.current_hash == comparison.current_hash
                and approval.proposed_hash == comparison.proposed_hash
            )
        }
        ledger, added = self.review_store.approve_targets(run_id, eligible)
        if added:
            self._save_execution_summary(run_id)
        return ledger, added

    def resolve_collection_item(
        self,
        run_id: str,
        action_id: str,
        item_id: str,
        decision: str,
        rationale: str,
        *,
        replacement_type_id: str | None = None,
    ) -> ExecutionReviewLedger:
        """Record an audited retain, remap or omission decision without writing."""

        decision = decision.strip().lower()
        rationale = rationale.strip()
        if decision not in {"retain", "remap", "omit"}:
            raise ValueError("Choose retain, remap or omit")
        if not rationale:
            raise ValueError("Enter a reason for this duplicate-type decision")
        action = next(
            (
                action
                for record in self._readiness_report(run_id).get("records", [])
                for action in record.get("actions", [])
                if action.get("action_id") == action_id
            ),
            None,
        )
        if action is None or item_id not in (action.get("proposed_item_ids") or []):
            raise ValueError("That proposed collection item does not exist in this run")
        resource = str(action.get("resource") or "")
        if resource not in {"address", "contact_detail", "court_opening_hours"}:
            raise ValueError("Only collection items can use duplicate-type resolution")
        if decision == "remap":
            replacement_type_id = (replacement_type_id or "").strip()
            if not self._archived_type_exists(run_id, resource, replacement_type_id):
                raise ValueError("Choose a type from the vocabulary archived for this run")
        else:
            replacement_type_id = None
        ledger = self.review_store.resolve_collection_item(
            run_id,
            CollectionItemResolution(
                item_id=item_id,
                action_id=action_id,
                resource=resource,
                decision=decision,
                rationale=rationale,
                replacement_type_id=replacement_type_id,
            ),
        )
        self._reset_dependent_actions(run_id, {action_id})
        self._save_execution_summary(run_id)
        return ledger

    def close_unactionable_court(
        self, run_id: str, source_row_number: int, rationale: str
    ) -> ExecutionReviewLedger:
        """Close an unmatched authoritative submission with an auditable reason."""

        rationale = rationale.strip()
        if not rationale:
            raise ValueError("Enter a reason for closing this court as unactionable")
        submission = self._submissions_by_row(run_id).get(source_row_number)
        if submission is None:
            raise ValueError("That source row does not exist in this run")
        if not any(
            issue.get("code") in {"COURT_SLUG_NOT_FOUND", "COURT_SLUG_SUGGESTED"}
            for issue in submission.get("issues", [])
        ):
            raise ValueError("Only a court without a defensible FaCT target can be closed")
        ledger = self.review_store.close_court(
            run_id,
            CourtDisposition(
                source_row_number=source_row_number,
                court_slug=submission.get("court_slug"),
                rationale=rationale,
            ),
        )
        self._save_execution_summary(run_id)
        return ledger

    def get_api_changes_review(self, run_id: str) -> dict[str, Any]:
        report = self._readiness_report(run_id)
        review = self.review_store.load(run_id)
        execution = self.store.load(run_id)
        approval_ledger = self.approval_store.load(run_id)
        llm_report = self._llm_review_report(run_id)
        obsolete_po_box_ids = {
            str(item["review_id"])
            for item in llm_report.get("items", [])
            if item.get("address_mode") == "po_box" and item.get("review_id")
        }
        review_ids_by_action: dict[str, set[str]] = {
            str(action.get("action_id") or ""): {
                str(review_id)
                for review_id in action.get("llm_review_ids", [])
                if review_id and str(review_id) not in obsolete_po_box_ids
            }
            for record in report.get("records", [])
            for action in record.get("actions", [])
        }
        for item in llm_report.get("items", []):
            if (
                not item.get("actionable")
                or item.get("address_mode") == "po_box"
                or not item.get("review_id")
            ):
                continue
            for action_id in item.get("dependent_action_ids", []):
                review_ids_by_action.setdefault(str(action_id), set()).add(
                    str(item["review_id"])
                )
        changes = []
        for record in report.get("records", []):
            court_slug = str(record.get("court_slug") or "")
            selected = review.source_selections.get(court_slug)
            for action in record.get("actions", []):
                change_id = target_change_id(court_slug, str(action.get("action_id") or ""))
                comparison = review.comparisons.get(change_id)
                approval = review.target_approvals.get(change_id)
                comparison_payload = comparison.model_dump(mode="json") if comparison else None
                if comparison_payload is not None:
                    comparison_payload["effective_differences"] = _component_differences(
                        comparison.current, comparison.proposed
                    )
                    comparison_payload["submitted_differences"] = _component_differences(
                        comparison.current, comparison.submitted
                    )
                    comparison_payload["differences"] = comparison_payload[
                        "effective_differences"
                    ]
                review_ids = sorted(
                    review_ids_by_action.get(str(action.get("action_id") or ""), set())
                )
                pending_value_holds = [
                    {
                        "review_id": review_id,
                        "kind": "llm",
                        "decision": (
                            "denied"
                            if review_id in approval_ledger.denials
                            else "pending"
                        ),
                    }
                    for review_id in review_ids
                    if review_id not in approval_ledger.approvals
                ]
                item_ids = action.get("proposed_item_ids") or []
                proposed_items = action.get("proposed_items") or []
                collection_items = [
                    {
                        "item_id": str(item_id),
                        "value": proposed_items[index]
                        if index < len(proposed_items)
                        else None,
                        "resolution": (
                            resolution.model_dump(mode="json")
                            if (
                                resolution := review.collection_item_resolutions.get(
                                    str(item_id)
                                )
                            )
                            else None
                        ),
                    }
                    for index, item_id in enumerate(item_ids)
                ]
                changes.append(
                    {
                        "change_id": change_id,
                        "court_slug": court_slug,
                        "source_row_numbers": record.get("source_row_numbers", []),
                        "source_row_number": action.get("source_row_number"),
                        "selected_source_row_number": (
                            selected.source_row_number if selected else None
                        ),
                        "source_selection_required": action.get(
                            "source_selection_required", False
                        ),
                        "action": action,
                        "comparison": comparison_payload,
                        "target_approved": bool(
                            approval
                            and comparison
                            and approval.current_hash == comparison.current_hash
                            and approval.proposed_hash == comparison.proposed_hash
                        ),
                        "pending_value_holds": pending_value_holds,
                        "collection_items": collection_items,
                        "type_options": self._archived_type_options(
                            run_id, str(action.get("resource") or "")
                        ),
                        "execution_status": self._effective_action_status(
                            run_id,
                            court_slug,
                            action,
                            execution,
                            approval_ledger,
                            llm_report,
                            review_state=review,
                            review_ids=review_ids,
                        ),
                    }
                )
        return {"run_id": run_id, "changes": changes}

    def get_llm_actions_review(self, run_id: str) -> dict[str, Any]:
        report = self._llm_review_report(run_id)
        execution_review = self.review_store.load(run_id)
        readiness = self._readiness_report(run_id, review_state=execution_review)
        approvals, added = self.approval_store.reconcile_policies(run_id, report, readiness)
        if added:
            self._save_execution_summary(run_id, approvals=approvals, review_report=report)
        execution = self.store.load(run_id)
        submissions_by_row = self._submissions_by_row(run_id)
        actions_by_id = {
            str(action.get("action_id")): (str(record.get("court_slug") or ""), action)
            for record in readiness.get("records", [])
            for action in record.get("actions", [])
            if action.get("action_id")
        }
        obsolete_po_box_ids = {
            str(item["review_id"])
            for item in report.get("items", [])
            if item.get("address_mode") == "po_box" and item.get("review_id")
        }
        review_ids_by_action: dict[str, set[str]] = {
            action_id: {
                str(review_id)
                for review_id in action.get("llm_review_ids", [])
                if review_id and str(review_id) not in obsolete_po_box_ids
            }
            for action_id, (_, action) in actions_by_id.items()
        }
        actions_by_review_id: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for court_slug, action in actions_by_id.values():
            for review_id in action.get("llm_review_ids", []):
                actions_by_review_id.setdefault(str(review_id), []).append(
                    (court_slug, action)
                )
        for value in report.get("items", []):
            if (
                not value.get("actionable")
                or value.get("address_mode") == "po_box"
                or not value.get("review_id")
            ):
                continue
            for action_id in value.get("dependent_action_ids", []):
                review_ids_by_action.setdefault(str(action_id), set()).add(
                    str(value["review_id"])
                )
        items = []
        for value in report.get("items", []):
            # Runs archived while PO Boxes had a short-lived manual-only policy
            # retain their immutable evidence, but that obsolete dependency is
            # deliberately absent from the mutable review and execution views.
            if value.get("address_mode") == "po_box":
                continue
            item = dict(value)
            item.setdefault("source_raw_values", {})
            item.setdefault("llm_input", {})
            item.setdefault("model_result", {})
            item["immutable_api_body_patch"] = item.get("api_body_patch")
            approved = item.get("review_id") in approvals.approvals
            approval = approvals.approvals.get(str(item.get("review_id") or ""))
            denial = approvals.denials.get(str(item.get("review_id") or ""))
            item["approved_at"] = approval.approved_at if approval is not None else None
            item["approval_method"] = approval.approval_method if approval else None
            item["approval_policy_version"] = approval.policy_version if approval else None
            item["approval_rationale"] = approval.rationale if approval else None
            item["approved_field_value"] = (
                approval.approved_field_value if approval else None
            )
            item["field_value_overridden"] = bool(
                approval and approval.field_value_overridden
            )
            item["omitted"] = bool(approval and approval.omitted)
            item["approved_address_patch"] = (
                approval.approved_address_patch if approval else None
            )
            item["decision_history"] = (
                [decision.model_dump(mode="json") for decision in approval.decision_history]
                if approval
                else []
            )
            item["denied_at"] = denial.denied_at if denial else None
            item["denial_rationale"] = denial.rationale if denial else None
            if approval and approval.approved_address_patch:
                item["api_body_patch"] = approval.approved_address_patch
                proposed = dict(item.get("proposed_address") or {})
                proposed.update(
                    {
                        "line_1": approval.approved_address_patch.get("addressLine1"),
                        "line_2": approval.approved_address_patch.get("addressLine2"),
                        "town_or_city": approval.approved_address_patch.get("townCity"),
                        "county": approval.approved_address_patch.get("county"),
                        "postcode": approval.approved_address_patch.get("postcode"),
                    }
                )
                item["proposed_address"] = proposed
            item["requires_manual_text_review"] = _requires_manual_text_review(item)
            item["field_editable"] = bool(
                _is_optional_explanation_review(item)
                and item.get("approval_status") not in {"already_executed"}
            )
            if approval and approval.field_value_overridden:
                item["approved_display_value"] = (
                    None if approval.omitted else approval.approved_field_value
                )
            linked_values = {
                str(action_id): linked
                for action_id in item.get("dependent_action_ids", [])
                if (linked := actions_by_id.get(str(action_id))) is not None
            }
            for court_slug, action in actions_by_review_id.get(
                str(item.get("review_id") or ""), []
            ):
                linked_values[str(action.get("action_id") or "")] = (court_slug, action)
            linked_actions = []
            for action_id, (court_slug, action) in linked_values.items():
                action_reason = _plain_action_reason(action.get("reason"))
                if (
                    not action_reason
                    and action.get("source_selection_required")
                    and court_slug not in execution_review.source_selections
                ):
                    action_reason = (
                        "A duplicate court submission needs an authoritative source row "
                        "to be selected."
                    )
                linked_actions.append(
                    {
                        "action_id": action_id,
                        "court_slug": court_slug,
                        "resource": action.get("resource"),
                        "status": self._effective_action_status(
                            run_id,
                            court_slug,
                            action,
                            execution,
                            approvals,
                            report,
                            review_state=execution_review,
                            review_ids=sorted(
                                review_ids_by_action.get(action_id, set())
                            ),
                        ),
                        "reason": action_reason,
                    }
                )
            linked_actions.sort(key=lambda action: str(action["action_id"]))
            item["dependent_action_ids"] = [
                action["action_id"] for action in linked_actions
            ]
            item["dependent_actions"] = linked_actions
            if linked_actions:
                item["actionable"] = True
            item["planning_blockers"] = _planning_blockers(
                item,
                linked_actions,
                submissions_by_row.get(item.get("source_row_number")),
            )
            submission = submissions_by_row.get(item.get("source_row_number"))
            item["court_target_resolution"] = _court_target_resolution_payload(
                submission,
                execution_review.court_target_overrides.get(
                    str(item.get("source_row_number") or "")
                ),
            )
            if "approvable" not in item:
                item["approvable"] = _is_usable_review_item(item)
            already_executed = bool(linked_actions) and all(
                action["status"] == "succeeded" for action in linked_actions
            )
            item["approval_status"] = (
                "approved"
                if approved
                else "denied"
                if denial
                else "already_executed"
                if item.get("actionable") and already_executed
                else "pending"
                if item.get("approvable", item.get("actionable"))
                else "not_actionable"
            )
            disposition = execution_review.court_dispositions.get(
                str(item.get("source_row_number") or "")
            )
            if disposition and item["approval_status"] != "already_executed":
                item["approval_status"] = "not_actionable"
                item["approvable"] = False
                item["actionable"] = False
                item["court_disposition"] = disposition.model_dump(mode="json")
            item["comparison_summary"] = _llm_item_comparison(item)
            items.append(item)
        return {**report, "items": items, "approval_counts": _approval_counts(items)}

    def reconcile_automatic_approvals(self, run_id: str) -> LlmApprovalLedger:
        """Apply versioned address and field policies without executing FaCT actions."""

        report = self._llm_review_report(run_id)
        approvals, added = self.approval_store.reconcile_policies(
            run_id, report, self._readiness_report(run_id)
        )
        if added:
            self._save_execution_summary(run_id, approvals=approvals, review_report=report)
        return approvals

    def approve_llm_review(
        self,
        run_id: str,
        review_id: str,
        *,
        address_patch: dict[str, str | None] | None = None,
        field_value: str | None = None,
        omit_field: bool = False,
        omission_rationale: str | None = None,
        execution_job_active: bool = False,
    ) -> LlmApprovalLedger:
        report = self._llm_review_report(run_id)
        current_item = next(
            (
                value
                for value in self.get_llm_actions_review(run_id).get("items", [])
                if value.get("review_id") == review_id
            ),
            None,
        )
        if current_item is None:
            raise ValueError(f"LLM review item '{review_id}' does not exist in run '{run_id}'")
        if (
            not current_item.get("approvable", current_item.get("actionable"))
            or current_item.get("outcome") != "accepted"
        ):
            raise ValueError("This LLM result is read-only and cannot be approved")
        if current_item and current_item.get("approval_status") == "already_executed":
            raise ValueError("This LLM result was already used by a succeeded API action")
        if current_item and current_item.get("approval_status") == "denied":
            raise ValueError("Reconsider this denied result before approving it")
        if current_item.get("kind") == "address":
            if execution_job_active:
                raise ValueError("Wait for the active execution job to finish before editing an address")
            patch = _normalise_reviewed_address_patch(
                address_patch or current_item.get("api_body_patch") or {}
            )
            prohibited = {
                action.get("status")
                for action in current_item.get("dependent_actions", [])
            } & {"running", "succeeded", "unknown"}
            if prohibited:
                raise ValueError(
                    "This address cannot be edited after execution starts, succeeds, or becomes uncertain"
                )
            previous = self.approval_store.load(run_id).approvals.get(review_id)
            immutable_patch = _normalise_reviewed_address_patch(
                current_item.get("immutable_api_body_patch") or {}
            )
            if (
                previous
                and previous.approval_method == "policy"
                and patch == immutable_patch
            ):
                ledger = self.approval_store.load(run_id)
            else:
                ledger = self.approval_store.approve_address(run_id, review_id, patch)
                action_ids = {
                    str(action_id)
                    for action_id in current_item.get("dependent_action_ids", [])
                    if action_id
                }
                self.review_store.invalidate_actions(run_id, action_ids)
                self._reset_dependent_actions(run_id, action_ids)
        elif address_patch is not None:
            raise ValueError("Address override fields are valid only for address reviews")
        elif current_item.get("field_editable"):
            if execution_job_active:
                raise ValueError(
                    "Wait for the active execution job to finish before editing this value"
                )
            prohibited = {
                action.get("status")
                for action in current_item.get("dependent_actions", [])
            } & {"running", "succeeded", "unknown"}
            if prohibited:
                raise ValueError(
                    "This value cannot be edited after execution starts, succeeds, or becomes uncertain"
                )
            ledger = self.approval_store.approve_field(
                run_id,
                review_id,
                field_value,
                omitted=omit_field,
                rationale=omission_rationale,
            )
            action_ids = {
                str(action_id)
                for action_id in current_item.get("dependent_action_ids", [])
                if action_id
            }
            self.review_store.invalidate_actions(run_id, action_ids)
            self._reset_dependent_actions(run_id, action_ids)
        elif field_value is not None or omit_field:
            raise ValueError("Editable field values are valid only for supported text reviews")
        else:
            ledger = self.approval_store.approve(run_id, review_id)
        self._save_execution_summary(run_id, approvals=ledger, review_report=report)
        return ledger

    def deny_llm_review(
        self, run_id: str, review_id: str, rationale: str
    ) -> LlmApprovalLedger:
        """Deny one pending usable result without executing a FaCT action."""

        report = self.get_llm_actions_review(run_id)
        current_item = next(
            (
                value
                for value in report.get("items", [])
                if value.get("review_id") == review_id
            ),
            None,
        )
        if current_item is None:
            raise ValueError(f"LLM review item '{review_id}' does not exist in run '{run_id}'")
        if current_item.get("approval_status") != "pending":
            raise ValueError("Only a pending LLM result can be denied")
        ledger = self.approval_store.deny(run_id, review_id, rationale)
        self._save_execution_summary(
            run_id,
            approvals=ledger,
            review_report=self._llm_review_report(run_id),
        )
        return ledger

    def reconsider_llm_review(self, run_id: str, review_id: str) -> LlmApprovalLedger:
        """Move one denied result back to pending without approving or executing it."""

        report = self.get_llm_actions_review(run_id)
        current_item = next(
            (
                value
                for value in report.get("items", [])
                if value.get("review_id") == review_id
            ),
            None,
        )
        if current_item is None:
            raise ValueError(f"LLM review item '{review_id}' does not exist in run '{run_id}'")
        if current_item.get("approval_status") != "denied":
            raise ValueError("Only a denied LLM result can be reconsidered")
        ledger, _ = self.approval_store.reconsider(run_id, review_id)
        self._save_execution_summary(
            run_id,
            approvals=ledger,
            review_report=self._llm_review_report(run_id),
        )
        return ledger

    def approve_all_pending_llm_reviews(
        self, run_id: str
    ) -> tuple[LlmApprovalLedger, int]:
        """Approve every remaining pending usable result atomically, without writing."""

        report = self.get_llm_actions_review(run_id)
        review_ids = {
            str(item["review_id"])
            for item in report.get("items", [])
            if item.get("review_id")
            and item.get("approval_status") == "pending"
            and not item.get("requires_manual_text_review")
        }
        ledger, added = self.approval_store.approve_many(run_id, review_ids)
        if added:
            self._save_execution_summary(
                run_id,
                approvals=ledger,
                review_report=self._llm_review_report(run_id),
            )
        return ledger, added

    def _submissions_by_row(self, run_id: str) -> dict[int, dict[str, Any]]:
        cached = self._submission_cache.get(run_id)
        if cached is not None:
            return cached
        archive = load_run_archive(self.output_root, run_id)
        if archive is None:
            return {}
        path = archive["path"] / "submissions_cleaned.json"
        if not path.exists():
            return {}
        try:
            submissions = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        result = {
            int(row): submission
            for submission in submissions
            if isinstance(submission, dict)
            and isinstance(
                row := submission.get("source", {}).get("source_row_number"), int
            )
        }
        self._submission_cache[run_id] = result
        return result

    def _reset_dependent_actions(self, run_id: str, action_ids: set[str]) -> None:
        if not action_ids:
            return
        ledger = self.store.load(run_id)
        changed = False
        report = self._readiness_report(run_id)
        for record in report.get("records", []):
            court_slug = str(record.get("court_slug") or "")
            court = ledger.courts.get(court_slug)
            if court is None:
                continue
            for action in record.get("actions", []):
                action_id = str(action.get("action_id") or "")
                state = court.actions.get(action_id)
                if action_id not in action_ids or state is None:
                    continue
                if state.status in {"running", "succeeded", "unknown"}:
                    continue
                state.status = "planned"
                state.reason = None
                state.last_checked_at = None
                state.last_response_status = None
                changed = True
            self._update_court_status(court, record.get("actions", []))
        if changed:
            self.store.save(ledger)

    def check_court(self, run_id: str, court_slug: str) -> ExecutionLedger:
        record = self._record(run_id, court_slug)
        ledger = self.store.load(run_id)
        client, close = self._client_or_new()
        try:
            self._preflight_actions(run_id, ledger, record, record.get("actions", []), client)
        finally:
            if close:
                client.close()
        return self._save_with_summary(run_id, ledger)

    def execute_action(self, run_id: str, court_slug: str, action_id: str) -> ExecutionLedger:
        self._require_writes_enabled()
        record = self._record(run_id, court_slug)
        action = next(
            (item for item in record.get("actions", []) if item.get("action_id") == action_id), None
        )
        if action is None:
            raise ValueError(f"Action '{action_id}' is not in court '{court_slug}'")
        self._require_selected_source(run_id, record, action)
        ledger = self.store.load(run_id)
        client, close = self._client_or_new()
        try:
            self._preflight_actions(run_id, ledger, record, [action], client)
            state = self._action_state(ledger, court_slug, action_id)
            if state.status == "ready":
                self._write_action(run_id, ledger, record, action, client)
        finally:
            if close:
                client.close()
        return self._save_with_summary(run_id, ledger)

    def execute_safe_court_actions(self, run_id: str, court_slug: str) -> ExecutionLedger:
        self._require_writes_enabled()
        record = self._record(run_id, court_slug)
        ledger = self.store.load(run_id)
        actions = self._active_actions(run_id, record)
        client, close = self._client_or_new()
        try:
            for action in actions:
                # Keep the live snapshot check immediately adjacent to its
                # mutation; a court-level job must not rely on an earlier scan.
                self._preflight_actions(run_id, ledger, record, [action], client)
                state = self._action_state(ledger, court_slug, action["action_id"])
                if state.status == "ready":
                    self._write_action(run_id, ledger, record, action, client)
        finally:
            if close:
                client.close()
        return self._save_with_summary(run_id, ledger)

    def execute_all_safe_actions(
        self,
        run_id: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> ExecutionLedger:
        """Execute up to four courts concurrently, preserving court action order."""

        self._require_writes_enabled()
        report = self._readiness_report(run_id)
        records = sorted(
            report.get("records", []), key=lambda record: str(record.get("court_slug") or "")
        )
        current = self.store.load(run_id)
        records = [
            record
            for record in records
            if self._batch_actions_to_attempt(
                current,
                {
                    **record,
                    "actions": self._active_actions(run_id, record),
                },
            )
        ]
        workers = (
            1
            if self._client is not None
            else self.config.fact_data_api_execution_concurrency
        )
        last_summary = time.monotonic()
        completed = 0
        if progress_callback:
            progress_callback(0, len(records))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="fact-court"
        ) as executor:
            futures = {
                executor.submit(self._execute_record_for_run, run_id, record): record
                for record in records
            }
            for future in as_completed(futures):
                future.result()
                completed += 1
                if progress_callback:
                    progress_callback(completed, len(records))
                now = time.monotonic()
                if completed % 10 == 0 or now - last_summary >= 10:
                    self._save_execution_summary(run_id, report=report)
                    last_summary = now
        ledger = self.store.load(run_id)
        return self._save_with_summary(run_id, ledger, report)

    def _execute_record_for_run(
        self, run_id: str, record: dict[str, Any]
    ) -> None:
        """Execute one court in isolation and merge only that court into state."""

        court_slug = str(record.get("court_slug") or "")
        current = self.store.load(run_id)
        ledger = ExecutionLedger(run_id=run_id)
        if court_slug in current.courts:
            ledger.courts[court_slug] = current.courts[court_slug].model_copy(deep=True)
        court = self._court_state(ledger, court_slug, record.get("court_id"))
        active_actions = self._active_actions(run_id, record)
        actions = self._batch_actions_to_attempt(
            ledger, {**record, "actions": active_actions}
        )
        if not actions:
            self._update_court_status(court, record.get("actions", []))
            self.store.save_court(run_id, court)
            return
        client, close = self._client_or_new()
        try:
            try:
                for action in actions:
                    self._preflight_actions(run_id, ledger, record, [action], client)
                    self.store.save_court(run_id, court)
                    state = self._action_state(
                        ledger, court_slug, str(action["action_id"])
                    )
                    if state.status == "ready":
                        self._write_action(run_id, ledger, record, action, client)
            except Exception as exc:
                self._record_unexpected_court_error(ledger, record, actions, exc)
        finally:
            self.store.save_court(run_id, court)
            if close:
                client.close()

    def get_execution_summary(self, run_id: str) -> dict[str, Any]:
        report = self._llm_review_report(run_id)
        readiness = self._readiness_report(run_id)
        approvals, _ = self.approval_store.reconcile_policies(run_id, report, readiness)
        summary = build_execution_summary(
            run_id,
            readiness,
            self.store.load(run_id),
            review_report=report,
            approvals=approvals,
            execution_review=self.review_store.load(run_id),
            submissions=self._review_summary_submissions(run_id),
        )
        return self.store.save_summary(run_id, summary)

    def get_cached_execution_summary(self, run_id: str) -> dict[str, Any]:
        """Read the mutation-refreshed summary, rebuilding only when absent or old."""

        summary = self.store.load_summary(run_id)
        if summary and summary.get("summary_version") == EXECUTION_SUMMARY_VERSION:
            return summary
        return self.get_execution_summary(run_id)

    def get_business_report(self, run_id: str) -> dict[str, Any]:
        self._readiness_report(run_id)
        return build_business_report(
            run_id,
            self.store.load(run_id),
            self.review_store.load(run_id),
            self.approval_store.load(run_id),
        )

    def _record(self, run_id: str, court_slug: str) -> dict[str, Any]:
        report = self._readiness_report(run_id)
        record = next(
            (item for item in report.get("records", []) if item.get("court_slug") == court_slug),
            None,
        )
        if record is None:
            raise ValueError(f"Court '{court_slug}' is not in the API readiness report")
        return record

    def _readiness_report(
        self,
        run_id: str,
        *,
        review_state: ExecutionReviewLedger | None = None,
    ) -> dict[str, Any]:
        archive = load_run_archive(self.output_root, run_id)
        if archive is None:
            raise ValueError(f"Run '{run_id}' does not exist")
        report_path = archive["path"] / "api_readiness_report.json"
        if not report_path.exists():
            raise ValueError("This archive does not contain an API readiness report")
        import json

        report = json.loads(report_path.read_text(encoding="utf-8"))
        latest_path = self.output_root / "latest_run.json"
        latest = (
            json.loads(latest_path.read_text(encoding="utf-8"))
            if latest_path.exists()
            else {}
        )
        execution = self.store.load(run_id)
        succeeded_action_ids = {
            action_id
            for court in execution.courts.values()
            for action_id, action in court.actions.items()
            if action.status == "succeeded"
        }
        existing_overlay = (
            self.output_root / "execution-review-state" / f"{run_id}.plan.json"
        ).exists()
        review_state = review_state or self.review_store.load(run_id)
        target_overrides = {
            int(row): CourtReference(
                court_id=override.target_court_id,
                slug=override.target_slug,
                name=override.target_court_name,
            )
            for row, override in review_state.court_target_overrides.items()
        }
        if (
            (latest.get("run_id") == run_id or existing_overlay or target_overrides)
            and (
                str(report.get("manifest_version") or "") < API_MANIFEST_VERSION
                or bool(target_overrides)
            )
            and (archive["path"] / "submissions_cleaned.json").exists()
        ):
            derived = derive_latest_execution_overlay(
                run_id,
                archive["path"],
                self.output_root,
                report,
                succeeded_action_ids,
                target_overrides,
            )
            if str(report.get("manifest_version") or "") < API_MANIFEST_VERSION:
                self.review_store.reconcile_plan_version(
                    run_id,
                    str(derived.get("manifest_version") or API_MANIFEST_VERSION),
                )
            return derived
        return report

    def _llm_review_report(self, run_id: str) -> dict[str, Any]:
        if run_id in self._llm_review_cache:
            return self._llm_review_cache[run_id]
        archive = load_run_archive(self.output_root, run_id)
        if archive is None:
            raise ValueError(f"Run '{run_id}' does not exist")
        report = load_or_derive_llm_actions_review(archive["path"])
        authoritative_rows = self._authoritative_source_rows(run_id)
        if authoritative_rows is not None:
            report = filter_llm_actions_review(report, authoritative_rows)
        self._llm_review_cache[run_id] = report
        return report

    def _authoritative_source_rows(self, run_id: str) -> set[int] | None:
        archive = load_run_archive(self.output_root, run_id)
        if archive is None:
            return None
        selection_path = archive["path"] / "submission_selection.json"
        if selection_path.exists():
            try:
                payload = json.loads(selection_path.read_text(encoding="utf-8"))
                return {
                    int(row) for row in payload.get("authoritative_source_row_numbers", [])
                }
            except (OSError, TypeError, ValueError):
                return None
        submissions_path = archive["path"] / "submissions_cleaned.json"
        if not submissions_path.exists():
            return None
        try:
            submissions = [
                CourtSubmission.model_validate(item)
                for item in json.loads(submissions_path.read_text(encoding="utf-8"))
            ]
        except (OSError, TypeError, ValueError):
            return None
        _, selection = select_authoritative_submissions(submissions)
        return set(selection["authoritative_source_row_numbers"])

    def _save_with_summary(
        self,
        run_id: str,
        ledger: ExecutionLedger,
        report: dict[str, Any] | None = None,
    ) -> ExecutionLedger:
        saved = self.store.save(ledger)
        self._save_execution_summary(
            run_id,
            ledger=saved,
            report=report,
        )
        return saved

    def _save_execution_summary(
        self,
        run_id: str,
        *,
        ledger: ExecutionLedger | None = None,
        report: dict[str, Any] | None = None,
        approvals: LlmApprovalLedger | None = None,
        review_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        summary = build_execution_summary(
            run_id,
            report or self._readiness_report(run_id),
            ledger or self.store.load(run_id),
            review_report=review_report or self._llm_review_report(run_id),
            approvals=approvals or self.approval_store.load(run_id),
            execution_review=self.review_store.load(run_id),
            submissions=self._review_summary_submissions(run_id),
        )
        return self.store.save_summary(run_id, summary)

    def _review_summary_submissions(self, run_id: str) -> list[dict[str, Any]]:
        submissions = self._submissions_by_row(run_id)
        authoritative = self._authoritative_source_rows(run_id)
        result = []
        for row, submission in submissions.items():
            if authoritative is not None and row not in authoritative:
                continue
            value = json.loads(json.dumps(submission))
            value["issues"] = [
                issue
                for issue in value.get("issues", [])
                if issue.get("code") != "DUPLICATE_COURT_SLUG"
            ]
            result.append(value)
        return result

    @staticmethod
    def _batch_actions_to_attempt(
        ledger: ExecutionLedger, record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        court = ledger.courts.get(str(record.get("court_slug") or ""))
        actions = []
        for action in record.get("actions", []):
            action_id = str(action.get("action_id") or "")
            state = court.actions.get(action_id) if court else None
            if state is None or state.status in {"planned", "awaiting_approval", "ready"}:
                actions.append(action)
        return actions

    def _record_unexpected_court_error(
        self,
        ledger: ExecutionLedger,
        record: dict[str, Any],
        actions: Iterable[dict[str, Any]],
        exc: Exception,
    ) -> None:
        court_slug = str(record.get("court_slug") or "")
        reason = f"Unexpected execution error ({type(exc).__name__}): {exc}"
        for action in actions:
            self._set_action(ledger, court_slug, action, "unknown", "execute", None, reason)
        self._update_court_status(self._court_state(ledger, court_slug), record.get("actions", []))

    def _client_or_new(self) -> tuple[FactApiExecutionClient, bool]:
        return (
            (self._client, False) if self._client else (FactApiExecutionClient(self.config), True)
        )

    def _require_writes_enabled(self) -> None:
        if not self.config.fact_data_api_writes_enabled:
            raise ValueError("FaCT API writes are disabled by FACT_DATA_API_WRITES_ENABLED")
        if not self.config.fact_data_api_user_id:
            raise ValueError(
                "FACT_DATA_API_USER_ID is required for audited FaCT API write requests"
            )
        try:
            UUID(self.config.fact_data_api_user_id)
        except ValueError as exc:
            raise ValueError("FACT_DATA_API_USER_ID must be a valid UUID") from exc

    def _preflight_actions(
        self,
        run_id: str,
        ledger: ExecutionLedger,
        record: dict[str, Any],
        actions: Iterable[dict[str, Any]],
        client: FactApiExecutionClient,
    ) -> None:
        actions = list(actions)
        court_slug = str(record["court_slug"])
        try:
            court = client.lookup_court(court_slug)
        except (httpx.HTTPError, ValueError) as exc:
            http_status, reason = _preflight_error_details("court lookup", exc)
            for action in actions:
                # A transient lookup outage must never erase a confirmed write
                # result or a deliberate earlier block from the ledger.
                existing = self._action_state(ledger, court_slug, str(action["action_id"]))
                if existing.status in {"succeeded", "blocked"}:
                    continue
                self._set_action(
                    ledger, court_slug, action, "unknown", "preflight", http_status, reason
                )
            self._update_court_status(self._court_state(ledger, court_slug), actions)
            return
        if court is None:
            for action in actions:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "preflight",
                    404,
                    "Court does not exist in FaCT",
                )
            self._update_court_status(self._court_state(ledger, court_slug), actions)
            return
        planned_id = record.get("court_id")
        if planned_id and planned_id != "{court_id}" and planned_id != court.court_id:
            for action in actions:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "preflight",
                    None,
                    "Court UUID no longer matches the reviewed report",
                )
            self._update_court_status(self._court_state(ledger, court_slug), actions)
            return
        court_state = self._court_state(ledger, court_slug, court.court_id)
        review_report = self._llm_review_report(run_id)
        approvals, _ = self.approval_store.reconcile_policies(
            run_id, review_report, self._readiness_report(run_id)
        )
        for action in actions:
            state = self._action_state(ledger, court_slug, action["action_id"])
            if state.status == "succeeded":
                continue
            if action.get("source_selection_required"):
                selected = self.review_store.load(run_id).source_selections.get(court_slug)
                if selected is None:
                    self._set_action(
                        ledger,
                        court_slug,
                        action,
                        "awaiting_approval",
                        "preflight",
                        None,
                        "Authoritative duplicate source row selection is required",
                    )
                    continue
                if selected.source_row_number != action.get("source_row_number"):
                    continue
            review_ids = self._action_review_ids(action, review_report)
            unapproved = [
                review_id for review_id in review_ids if review_id not in approvals.approvals
            ]
            if unapproved:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "awaiting_approval",
                    "preflight",
                    None,
                    f"LLM approval required for {len(unapproved)} field result(s)",
                )
                continue
            approved_address = self._approved_address_item(action, review_report, approvals)
            request_defaults = self._request_only_defaults(run_id, action)
            legacy_reason_is_resolved = bool(
                approved_address and _legacy_address_reason_is_only_blocker(action)
            ) or _legacy_lift_reason_is_only_blocker(action, request_defaults)
            collection = action.get("resource") in {
                "address",
                "contact_detail",
                "court_opening_hours",
            }
            bodies = (
                self._execution_items(run_id, action, court.court_id)
                if collection
                else [
                    self._execution_body(
                        run_id,
                        action,
                        court.court_id,
                        approved_address=approved_address,
                    )
                ]
            )
            duplicate_reason_is_resolved = bool(
                collection
                and _reason_contains_only_duplicate_type_conflicts(
                    str(action.get("reason") or "")
                )
                and not merged_target_state(action, [], bodies)[2]
            )
            if action.get("readiness") != "ready" and not (
                legacy_reason_is_resolved or duplicate_reason_is_resolved
            ):
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "preflight",
                    None,
                    str(action.get("reason") or "Action body is not ready for FaCT"),
                )
                continue
            if not bodies:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "preflight",
                    None,
                    "Action body cannot be sent to FaCT: empty section",
                )
                continue
            approved_addresses = self._approved_address_items(
                action, review_report, approvals, len(bodies)
            )
            verifications = action.get("address_verifications") or []
            for index, body in enumerate(bodies):
                address_action = action
                if index < len(verifications) and isinstance(verifications[index], dict):
                    address_action = {**action, "address_verification": verifications[index]}
                with self._postcode_lock:
                    self._postcode_lookup.set_lookup(
                        lambda postcode: client.get(
                            f"/search/address/v1/postcode/{quote(postcode, safe='')}"
                        )
                    )
                    address_result = _address_os_preflight_result(
                        address_action,
                        body,
                        self._postcode_lookup.get,
                        approved_address=approved_addresses[index],
                    )
                if address_result.status != "ready":
                    self._set_action(
                        ledger,
                        court_slug,
                        action,
                        address_result.status,
                        "preflight",
                        address_result.http_status,
                        address_result.reason,
                    )
                    break
            else:
                address_result = AddressPreflightResult("ready")
            if address_result.status != "ready":
                continue
            try:
                target = client.get(_preflight_path(str(action["path"])))
            except httpx.HTTPError as exc:
                http_status, reason = _preflight_error_details("target section check", exc)
                self._set_action(
                    ledger, court_slug, action, "unknown", "preflight", http_status, reason
                )
                continue
            effective_action = dict(action)
            if collection:
                effective_action["proposed_items"] = bodies
            else:
                effective_action["body"] = bodies[0]
            if target.status_code in {200, 204, 404}:
                comparison = build_target_comparison(
                    court_slug, effective_action, target.body
                )
                review_state = self.review_store.save_comparison(run_id, comparison)
            else:
                comparison = None
                review_state = self.review_store.load(run_id)
            operation_reason = (
                _comparison_operation_validation_reason(action, comparison, court.court_id)
                if comparison
                else None
            )
            if comparison and comparison.merge_conflicts:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "preflight",
                    target.status_code,
                    "; ".join(comparison.merge_conflicts),
                )
            elif comparison and operation_reason:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "preflight",
                    target.status_code,
                    f"Merged action cannot be sent to FaCT: {operation_reason}",
                )
            elif comparison and comparison.is_no_change:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "succeeded",
                    "preflight",
                    target.status_code,
                    "FaCT already contains the effective merged section; no write was required",
                )
            elif comparison and comparison.has_existing_data:
                target_approval = review_state.target_approvals.get(comparison.change_id)
                if not target_approval:
                    self._set_action(
                        ledger,
                        court_slug,
                        action,
                        "awaiting_approval",
                        "preflight",
                        target.status_code,
                        "Changes to the existing FaCT section require approval of the displayed before and effective after values",
                    )
                else:
                    self._set_action(
                        ledger, court_slug, action, "ready", "preflight", target.status_code, None
                    )
            elif comparison:
                self._set_action(
                    ledger, court_slug, action, "ready", "preflight", target.status_code, None
                )
            else:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "unknown",
                    "preflight",
                    target.status_code,
                    "Target section preflight returned an unexpected response",
                )
        self._update_court_status(court_state, self._active_actions(run_id, record))

    def _write_action(
        self,
        run_id: str,
        ledger: ExecutionLedger,
        record: dict[str, Any],
        action: dict[str, Any],
        client: FactApiExecutionClient,
    ) -> None:
        court_slug = str(record["court_slug"])
        state = self._action_state(ledger, court_slug, action["action_id"])
        state.status = "running"
        persistence_seconds = 0.0
        request_seconds = 0.0
        write_request_count = 0
        accepted_write_count = 0
        rejected_write_count = 0
        unknown_write_count = 0
        persist_started = time.monotonic()
        self.store.save_court(run_id, self._court_state(ledger, court_slug))
        persistence_seconds += time.monotonic() - persist_started
        court_id = self._court_state(ledger, court_slug).court_id
        if not court_id:
            self._set_action(
                ledger,
                court_slug,
                action,
                "unknown",
                "execute",
                None,
                "Court UUID was unavailable after a successful preflight",
            )
        else:
            change_id = target_change_id(court_slug, str(action["action_id"]))
            comparison = self.review_store.load(run_id).comparisons.get(change_id)
            if comparison is None:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "execute",
                    None,
                    "Live FaCT comparison is missing; run preflight again",
                )
            else:
                completed_operations = 0
                for operation in comparison.operations:
                    method = str(operation.get("method") or "")
                    body = dict(operation.get("body") or {})
                    if method != "DELETE":
                        if action.get("resource") != "professional_information":
                            body["courtId"] = court_id
                        body = normalise_fact_api_action_body(
                            str(action.get("resource") or ""), body
                        )
                        body_reason = validate_fact_api_action_body(
                            str(action.get("resource") or ""), body
                        )
                        if body_reason:
                            self._set_action(
                                ledger,
                                court_slug,
                                action,
                                "blocked",
                                "execute",
                                None,
                                f"Merged operation cannot be sent to FaCT: {body_reason}",
                            )
                            break
                    try:
                        request_started = time.monotonic()
                        write_request_count += 1
                        response = client.write(method, str(operation.get("path") or ""), body)
                        request_seconds += time.monotonic() - request_started
                    except httpx.TimeoutException:
                        request_seconds += time.monotonic() - request_started
                        unknown_write_count += 1
                        self._set_action(
                            ledger,
                            court_slug,
                            action,
                            "unknown",
                            "execute",
                            None,
                            "Merged section update timed out; outcome is unknown",
                        )
                        break
                    except httpx.HTTPError as exc:
                        request_seconds += time.monotonic() - request_started
                        http_status, reason = _preflight_error_details(
                            "merged section request", exc
                        )
                        if http_status is None:
                            unknown_write_count += 1
                        else:
                            rejected_write_count += 1
                        self._set_action(
                            ledger, court_slug, action, "failed", "execute", http_status, reason
                        )
                        break
                    if not 200 <= response.status_code < 300:
                        rejected_write_count += 1
                        self._set_action(
                            ledger,
                            court_slug,
                            action,
                            "failed",
                            "execute",
                            response.status_code,
                            _write_rejection_reason(response.status_code, response.body),
                        )
                        break
                    accepted_write_count += 1
                    completed_operations += 1
                    persist_started = time.monotonic()
                    self.store.save_court(
                        run_id, self._court_state(ledger, court_slug)
                    )
                    persistence_seconds += time.monotonic() - persist_started
                else:
                    self._set_action(
                        ledger,
                        court_slug,
                        action,
                        "succeeded",
                        "execute",
                        200,
                        f"Completed {completed_operations} reviewed merged-section operation(s)",
                    )
                if state.status in {"blocked", "failed", "unknown"} and (
                    state.status == "unknown" or completed_operations
                ):
                    self._capture_partial_section_state(
                        run_id,
                        ledger,
                        court_slug,
                        action,
                        comparison,
                        completed_operations,
                        client,
                    )
        execute_attempt = next(
            (attempt for attempt in reversed(state.attempts) if attempt.operation == "execute"),
            None,
        )
        if execute_attempt:
            execute_attempt.request_duration_ms = round(
                request_seconds * 1000, 3
            )
            execute_attempt.persistence_duration_ms = round(
                persistence_seconds * 1000, 3
            )
            execute_attempt.write_request_count = write_request_count
            execute_attempt.accepted_write_count = accepted_write_count
            execute_attempt.rejected_write_count = rejected_write_count
            execute_attempt.unknown_write_count = unknown_write_count
        self._update_court_status(
            self._court_state(ledger, court_slug), self._active_actions(run_id, record)
        )

    def _capture_partial_section_state(
        self,
        run_id: str,
        ledger: ExecutionLedger,
        court_slug: str,
        action: dict[str, Any],
        comparison: TargetComparison,
        completed_operations: int,
        client: FactApiExecutionClient,
    ) -> None:
        state = self._action_state(ledger, court_slug, str(action["action_id"]))
        try:
            response = client.get(_preflight_path(str(action["path"])))
        except (httpx.HTTPError, RuntimeError, ValueError):
            note = "Live FaCT could not be re-read after the partial merged update."
        else:
            if response.status_code in {200, 204, 404}:
                effective_action = dict(action)
                court_id = self._court_state(ledger, court_slug).court_id or "{court_id}"
                if action.get("resource") in {
                    "address",
                    "contact_detail",
                    "court_opening_hours",
                }:
                    effective_action["proposed_items"] = self._execution_items(
                        run_id, action, court_id
                    )
                else:
                    effective_action["body"] = self._execution_body(
                        run_id, action, court_id
                    )
                refreshed = build_target_comparison(
                    court_slug, effective_action, response.body
                )
                self.review_store.save_comparison(run_id, refreshed)
                note = (
                    "Live FaCT was re-read after the partial merged update; "
                    "the change approval was invalidated for a new review."
                )
            else:
                note = (
                    "Live FaCT returned an unexpected response when re-read after "
                    "the partial merged update."
                )
        state.reason = f"{state.reason or 'Merged section update stopped.'} {note}"
        state.attempts.append(
            ActionAttempt(
                operation="preflight",
                outcome=state.status,
                http_status=None,
                message=f"{completed_operations} merged operation(s) completed. {note}",
            )
        )

    def _execution_body(
        self,
        run_id: str,
        action: dict[str, Any],
        court_id: str,
        *,
        approved_address: dict[str, Any] | None | object = _ADDRESS_REVIEW_NOT_LOADED,
    ) -> dict[str, Any]:
        """Use the freshly resolved UUID without modifying the immutable report."""

        body = dict(action.get("body") or {})
        if action.get("resource") == "address" and approved_address is _ADDRESS_REVIEW_NOT_LOADED:
            review_report = self._llm_review_report(run_id)
            approvals, _ = self.approval_store.reconcile_policies(
                run_id, review_report, self._readiness_report(run_id)
            )
            approved_address = self._approved_address_item(
                action,
                review_report,
                approvals,
            )
        if isinstance(approved_address, dict):
            for field, value in (approved_address.get("api_body_patch") or {}).items():
                if value is None:
                    body.pop(field, None)
                else:
                    body[field] = value
        body.update(self._request_only_defaults(run_id, action))
        if action.get("resource") != "professional_information":
            body["courtId"] = court_id
        return normalise_fact_api_action_body(str(action.get("resource") or ""), body)

    def _execution_items(
        self, run_id: str, action: dict[str, Any], court_id: str
    ) -> list[dict[str, Any]]:
        proposed = action.get("proposed_items")
        items = [dict(item) for item in proposed] if isinstance(proposed, list) else []
        if not items:
            items = [dict(action.get("body") or {})]
        if action.get("resource") == "address":
            review_report = self._llm_review_report(run_id)
            approvals, _ = self.approval_store.reconcile_policies(
                run_id, review_report, self._readiness_report(run_id)
            )
            review_items = self._approved_address_items(
                action, review_report, approvals, len(items)
            )
            for index, review_item in enumerate(review_items):
                for field, value in (review_item or {}).get("api_body_patch", {}).items():
                    if value is None:
                        items[index].pop(field, None)
                    else:
                        items[index][field] = value
        if action.get("resource") == "contact_detail":
            self._apply_approved_contact_explanations(run_id, action, items)
        items = self._apply_collection_item_resolutions(run_id, action, items)
        request_defaults = self._request_only_defaults(run_id, action)
        for item in items:
            item.update(request_defaults)
            if action.get("resource") != "professional_information":
                item["courtId"] = court_id
        return [
            normalise_fact_api_action_body(str(action.get("resource") or ""), item)
            for item in items
        ]

    def _apply_collection_item_resolutions(
        self,
        run_id: str,
        action: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        item_ids = action.get("proposed_item_ids") or []
        if not isinstance(item_ids, list):
            return items
        resolutions = self.review_store.load(run_id).collection_item_resolutions
        result: list[dict[str, Any]] = []
        type_field = {
            "address": "addressType",
            "contact_detail": "courtContactDescriptionId",
            "court_opening_hours": "openingHourTypeId",
        }.get(str(action.get("resource") or ""))
        for index, item in enumerate(items):
            item_id = str(item_ids[index]) if index < len(item_ids) else ""
            resolution = resolutions.get(item_id)
            if resolution and resolution.decision == "omit":
                continue
            resolved = dict(item)
            if (
                resolution
                and resolution.decision == "remap"
                and resolution.replacement_type_id
                and type_field
            ):
                resolved[type_field] = resolution.replacement_type_id
            result.append(resolved)
        return result

    def _archived_type_exists(
        self, run_id: str, resource: str, type_id: str
    ) -> bool:
        return any(
            option["api_id"] == type_id
            for option in self._archived_type_options(run_id, resource)
        )

    def _archived_type_options(
        self, run_id: str, resource: str
    ) -> list[dict[str, str]]:
        vocabulary_name = {
            "address": "address_types",
            "contact_detail": "contact_description_types",
            "court_opening_hours": "opening_hour_types",
        }.get(resource)
        archive = load_run_archive(self.output_root, run_id)
        if archive is None or vocabulary_name is None:
            return []
        path = archive["path"] / "fact_vocabularies.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = payload.get("vocabularies", {}).get(vocabulary_name, [])
        return [
            {"api_id": str(entry["api_id"]), "name": str(entry.get("name") or "")}
            for entry in entries
            if entry.get("api_id")
        ]

    def _apply_approved_contact_explanations(
        self,
        run_id: str,
        action: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> None:
        """Overlay reviewed explanation edits without changing archived evidence."""

        source_fields = action.get("proposed_item_source_fields") or []
        if not isinstance(source_fields, list):
            return
        approvals = self.approval_store.load(run_id)
        report = self._llm_review_report(run_id)
        review_by_field = {
            str(item.get("field")): str(item.get("review_id"))
            for item in report.get("items", [])
            if item.get("kind") == "field"
            and str(item.get("field") or "").endswith(".explanation")
            and item.get("review_id")
        }
        for index, item_fields in enumerate(source_fields[: len(items)]):
            if not isinstance(item_fields, list):
                continue
            review_id = next(
                (
                    review_id
                    for review_field, review_id in review_by_field.items()
                    if any(
                        review_field == str(source_field)
                        or review_field.startswith(f"{source_field}.")
                        for source_field in item_fields
                    )
                ),
                None,
            )
            if review_id is None:
                continue
            approval = approvals.approvals.get(review_id)
            if not approval or not approval.field_value_overridden:
                continue
            if approval.omitted:
                items[index].pop("explanation", None)
            else:
                items[index]["explanation"] = approval.approved_field_value

    def _request_only_defaults(
        self, run_id: str, action: dict[str, Any]
    ) -> dict[str, int]:
        source_row = action.get("source_row_number")
        if not isinstance(source_row, int):
            return {}
        submission = self._submissions_by_row(run_id).get(source_row)
        return _legacy_lift_request_defaults(action, submission)

    def _active_actions(
        self, run_id: str, record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        actions = list(record.get("actions", []))
        dispositions = self.review_store.load(run_id).court_dispositions
        if any(
            str(row) in dispositions
            for row in record.get("source_row_numbers", [])
        ):
            return []
        if not any(action.get("source_selection_required") for action in actions):
            return actions
        selected = self.review_store.load(run_id).source_selections.get(
            str(record.get("court_slug") or "")
        )
        if selected is None:
            return actions
        return [
            action
            for action in actions
            if not action.get("source_selection_required")
            or action.get("source_row_number") == selected.source_row_number
        ]

    def _require_selected_source(
        self, run_id: str, record: dict[str, Any], action: dict[str, Any]
    ) -> None:
        if not action.get("source_selection_required"):
            return
        selected = self.review_store.load(run_id).source_selections.get(
            str(record.get("court_slug") or "")
        )
        if selected is None:
            raise ValueError("Select the authoritative duplicate source row before continuing")
        if selected.source_row_number != action.get("source_row_number"):
            raise ValueError("This action belongs to a duplicate source row that was not selected")

    @staticmethod
    def _action_review_ids(action: dict[str, Any], review_report: dict[str, Any]) -> list[str]:
        action_id = str(action.get("action_id") or "")
        obsolete_po_box_ids = {
            str(item["review_id"])
            for item in review_report.get("items", [])
            if item.get("address_mode") == "po_box" and item.get("review_id")
        }
        explicit = {
            str(review_id)
            for review_id in action.get("llm_review_ids", [])
            if review_id and str(review_id) not in obsolete_po_box_ids
        }
        derived = {
            str(item["review_id"])
            for item in review_report.get("items", [])
            if item.get("actionable")
            and item.get("address_mode") != "po_box"
            and action_id in item.get("dependent_action_ids", [])
            and item.get("review_id")
        }
        return sorted(explicit | derived)

    def _approved_address_item(
        self,
        action: dict[str, Any],
        review_report: dict[str, Any],
        approvals: LlmApprovalLedger,
    ) -> dict[str, Any] | None:
        if action.get("resource") != "address":
            return None
        review_ids = set(self._action_review_ids(action, review_report))
        item = next(
            (
                item
                for item in review_report.get("items", [])
                if item.get("kind") == "address"
                and item.get("outcome") == "accepted"
                and item.get("review_id") in review_ids
                and item.get("review_id") in approvals.approvals
                and item.get("api_body_patch")
            ),
            None,
        )
        return _review_item_with_approved_patch(item, approvals) if item else None

    def _approved_address_items(
        self,
        action: dict[str, Any],
        review_report: dict[str, Any],
        approvals: LlmApprovalLedger,
        item_count: int,
    ) -> list[dict[str, Any] | None]:
        """Align approved OS selections with the proposed address collection."""

        aligned: list[dict[str, Any] | None] = [None] * item_count
        if action.get("resource") != "address" or not item_count:
            return aligned
        review_ids = set(self._action_review_ids(action, review_report))
        candidates = [
            _review_item_with_approved_patch(item, approvals)
            for item in review_report.get("items", [])
            if item.get("kind") == "address"
            and item.get("outcome") == "accepted"
            and item.get("review_id") in review_ids
            and item.get("review_id") in approvals.approvals
            and item.get("api_body_patch")
        ]
        verifications = action.get("address_verifications") or []
        for index, verification in enumerate(verifications[:item_count]):
            if not isinstance(verification, dict):
                continue
            address_index = verification.get("address_index")
            aligned[index] = next(
                (
                    item
                    for item in candidates
                    if item.get("source_row_number") == action.get("source_row_number")
                    and item.get("address_index") == address_index
                ),
                None,
            )
        # Legacy single-address actions did not retain per-address evidence.
        if item_count == 1 and aligned[0] is None and candidates:
            aligned[0] = candidates[0]
        return aligned

    def _effective_action_status(
        self,
        run_id: str,
        court_slug: str,
        action: dict[str, Any],
        ledger: ExecutionLedger,
        approvals: LlmApprovalLedger,
        review_report: dict[str, Any],
        *,
        review_state: ExecutionReviewLedger | None = None,
        review_ids: list[str] | None = None,
    ) -> str:
        court = ledger.courts.get(court_slug)
        state = court.actions.get(str(action.get("action_id"))) if court else None
        status = state.status if state else "planned"
        if status in {"blocked", "failed", "unknown", "running", "succeeded"}:
            return status
        review_state = review_state or self.review_store.load(run_id)
        if action.get("source_selection_required"):
            selection = review_state.source_selections.get(court_slug)
            if selection is None:
                return "awaiting_approval"
            if selection.source_row_number != action.get("source_row_number"):
                return "not_selected"
        review_ids = (
            review_ids
            if review_ids is not None
            else self._action_review_ids(action, review_report)
        )
        if any(review_id not in approvals.approvals for review_id in review_ids):
            return "awaiting_approval"
        comparison = review_state.comparisons.get(
            target_change_id(court_slug, str(action.get("action_id") or ""))
        )
        if comparison and comparison.has_existing_data and not comparison.is_no_change:
            target_approval = review_state.target_approvals.get(comparison.change_id)
            if not target_approval:
                return "awaiting_approval"
        return "planned" if status == "awaiting_approval" else status

    def _court_state(
        self, ledger: ExecutionLedger, slug: str, court_id: str | None = None
    ) -> CourtExecutionState:
        if slug not in ledger.courts:
            ledger.courts[slug] = CourtExecutionState(court_slug=slug, court_id=court_id)
        state = ledger.courts[slug]
        if court_id:
            state.court_id = court_id
        return state

    def _action_state(
        self, ledger: ExecutionLedger, slug: str, action_id: str
    ) -> ActionExecutionState:
        court = self._court_state(ledger, slug)
        if action_id not in court.actions:
            court.actions[action_id] = ActionExecutionState(action_id=action_id)
        return court.actions[action_id]

    def _set_action(
        self,
        ledger: ExecutionLedger,
        slug: str,
        action: dict[str, Any],
        status: str,
        operation: str,
        http_status: int | None,
        reason: str | None,
    ) -> None:
        state = self._action_state(ledger, slug, str(action["action_id"]))
        state.status = status  # type: ignore[assignment]
        state.last_checked_at = utc_now()
        state.last_response_status = http_status
        state.reason = reason
        state.attempts.append(
            ActionAttempt(
                operation=operation, outcome=status, http_status=http_status, message=reason
            )
        )

    def _update_court_status(
        self, court: CourtExecutionState, actions: Iterable[dict[str, Any]]
    ) -> None:
        action_ids = [str(action["action_id"]) for action in actions]
        statuses = [
            court.actions[action_id].status
            for action_id in action_ids
            if action_id in court.actions
        ]
        if (
            action_ids
            and len(statuses) == len(action_ids)
            and all(status == "succeeded" for status in statuses)
        ):
            court.status = "completed"
        elif any(status in {"blocked", "failed", "unknown"} for status in statuses):
            court.status = "attention_required"
        elif any(status == "awaiting_approval" for status in statuses):
            court.status = "awaiting_approval"
        elif statuses:
            court.status = "in_progress"
        else:
            court.status = "not_started"


def _approval_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    actionable = [item for item in items if item.get("actionable")]
    approved = [item for item in actionable if item.get("approval_status") == "approved"]
    reviewable = [
        item for item in items if item.get("approvable", item.get("actionable"))
    ]
    policy_approved = [
        item
        for item in reviewable
        if item.get("approval_status") == "approved"
        and item.get("approval_method") == "policy"
    ]
    return {
        "total": len(actionable),
        "approved": len(approved),
        "manual_approved": sum(item.get("approval_method") == "manual" for item in approved),
        "auto_approved": sum(item.get("approval_method") == "policy" for item in approved),
        "auto_approved_total": len(policy_approved),
        "auto_approved_addresses": sum(
            item.get("approval_policy_version")
            in {"high-single-os-candidate-v1", "high-supplied-os-candidate-v2"}
            for item in policy_approved
        ),
        "auto_approved_unchanged_fields": sum(
            item.get("approval_policy_version") == "high-unchanged-field-v1"
            for item in policy_approved
        ),
        "auto_approved_fields": sum(
            item.get("approval_policy_version")
            in {"high-unchanged-field-v1", "high-accepted-field-v2"}
            for item in policy_approved
        ),
        "pending": sum(item.get("approval_status") == "pending" for item in actionable),
        "denied": sum(item.get("approval_status") == "denied" for item in actionable),
        "already_executed": sum(
            item.get("approval_status") == "already_executed" for item in actionable
        ),
        "not_actionable": sum(not item.get("actionable") for item in items),
        "reviewable_total": len(reviewable),
        "review_pending": sum(
            item.get("approval_status") == "pending" for item in reviewable
        ),
        "review_denied": sum(
            item.get("approval_status") == "denied" for item in reviewable
        ),
    }


def _llm_item_comparison(item: dict[str, Any]) -> dict[str, Any]:
    result = item.get("model_result") or {}
    if item.get("kind") == "address":
        before = item.get("submitted_address") or {}
        after = item.get("proposed_address") or {}
        components = []
        for key in (
            "line_1",
            "line_2",
            "town_or_city",
            "county",
            "postcode",
            "address_type",
        ):
            old = before.get(key)
            new = after.get(key)
            if old != new:
                components.append(
                    {
                        "field": key,
                        "before": old,
                        "after": new,
                        "weaker_signal": key == "line_1",
                    }
                )
        tag = (
            "no_selection"
            if not result.get("uprn")
            else "multiple_candidates"
            if len((item.get("llm_input") or {}).get("candidates", [])) > 1
            else "changed"
            if components
            else "unchanged"
        )
        return {"tag": tag, "components": components}

    before = (item.get("llm_input") or {}).get("cleaned_value")
    if before is None:
        before = (item.get("llm_input") or {}).get("raw_value")
    after = result.get("value")
    operation = result.get("operation")
    if operation == "clear":
        tag = "cleared"
    elif after is None:
        tag = "no_selection"
    elif str(before) == str(after):
        tag = "unchanged"
    elif " ".join(str(before).split()).casefold() == " ".join(str(after).split()).casefold():
        tag = "format_only"
    else:
        tag = "changed"
    before_parts, after_parts = _word_diff(before, after)
    return {
        "tag": tag,
        "before": before,
        "after": after,
        "before_parts": before_parts,
        "after_parts": after_parts,
    }


def _word_diff(before: Any, after: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def text(value: Any) -> str:
        if isinstance(value, (dict, list)):
            import json

            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return "" if value is None else str(value)

    old_tokens = re.findall(r"\w+|[^\w\s]+|\s+", text(before))
    new_tokens = re.findall(r"\w+|[^\w\s]+|\s+", text(after))
    old_parts: list[dict[str, Any]] = []
    new_parts: list[dict[str, Any]] = []
    for operation, old_start, old_end, new_start, new_end in SequenceMatcher(
        None, old_tokens, new_tokens
    ).get_opcodes():
        old_parts.append(
            {
                "text": "".join(old_tokens[old_start:old_end]),
                "changed": operation in {"replace", "delete"},
            }
        )
        new_parts.append(
            {
                "text": "".join(new_tokens[new_start:new_end]),
                "changed": operation in {"replace", "insert"},
            }
        )
    return old_parts, new_parts


def _component_differences(current: Any, proposed: Any) -> list[dict[str, Any]]:
    def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, child in value.items():
                if key in {"id", "createdAt", "updatedAt"}:
                    continue
                path = f"{prefix}.{key}" if prefix else str(key)
                result.update(flatten(child, path))
            return result
        if isinstance(value, list):
            result = {}
            for index, child in enumerate(value):
                result.update(flatten(child, f"{prefix}[{index}]"))
            return result
        return {prefix or "value": value}

    before = flatten(current)
    after = flatten(proposed)
    return [
        {"field": field, "before": before.get(field), "after": after.get(field)}
        for field in sorted(set(before) | set(after))
        if before.get(field) != after.get(field)
    ]


def _preflight_path(action_path: str) -> str:
    """Collection endpoints are safe to GET before deciding whether to create."""

    return action_path


def _target_has_existing_data(status_code: int, body: Any) -> bool:
    if status_code == 204 or status_code == 404 or body is None:
        return False
    if status_code != 200:
        return False
    if isinstance(body, list):
        return bool(body)
    if isinstance(body, dict):
        return bool(body)
    return bool(body)


def _write_rejection_reason(status_code: int, body: Any) -> str:
    """Keep API validation feedback useful without copying arbitrary response bodies."""

    prefix = f"FaCT API rejected the write request (HTTP {status_code})"
    if status_code != 400 or not isinstance(body, dict):
        return prefix

    field_errors = _validation_error_messages(body)
    return f"{prefix}: {'; '.join(field_errors)}" if field_errors else prefix


def _validation_error_messages(body: dict[str, Any]) -> list[str]:
    """Extract a short, safe summary from known FaCT validation response shapes."""

    messages: list[str] = []

    def add(field: str | None, value: Any) -> None:
        if len(messages) >= 3 or not isinstance(value, str) or not value.strip():
            return
        prefix = f"{field}: " if field else ""
        messages.append(f"{prefix}{value.strip()[:300]}")

    add(None, body.get("message"))
    for field, value in body.items():
        if field in {"timestamp", "message", "error", "errors", "fieldErrors", "details"}:
            continue
        add(str(field), value)

    for key in ("errors", "fieldErrors", "details"):
        nested = body.get(key)
        if isinstance(nested, dict):
            for field, value in nested.items():
                add(str(field), value)
        elif isinstance(nested, list):
            for item in nested:
                if isinstance(item, str):
                    add(None, item)
                elif isinstance(item, dict):
                    add(str(item.get("field") or item.get("path") or "error"), item.get("message"))
    return messages


def _is_usable_review_item(item: dict[str, Any]) -> bool:
    if item.get("outcome") != "accepted" or not item.get("review_id"):
        return False
    if item.get("kind") == "address":
        result = item.get("model_result")
        return isinstance(result, dict) and bool(result.get("uprn"))
    result = item.get("model_result")
    if not isinstance(result, dict):
        return False
    operation = result.get("operation") or item.get("operation")
    return operation == "clear" or result.get("value") is not None


def _review_item_with_approved_patch(
    item: dict[str, Any], approvals: LlmApprovalLedger
) -> dict[str, Any]:
    approval = approvals.approvals.get(str(item.get("review_id") or ""))
    if approval is None or approval.approved_address_patch is None:
        return item
    return {**item, "api_body_patch": approval.approved_address_patch}


_ADDRESS_PATCH_FIELDS = (
    "addressLine1",
    "addressLine2",
    "townCity",
    "county",
    "postcode",
)


def _normalise_reviewed_address_patch(
    value: dict[str, Any],
) -> dict[str, str | None]:
    patch: dict[str, Any] = {}
    for field in _ADDRESS_PATCH_FIELDS:
        raw = value.get(field)
        patch[field] = raw.strip() if isinstance(raw, str) and raw.strip() else None
    candidate = normalise_fact_api_action_body(
        "address",
        {
            "courtId": "{court_id}",
            "addressType": "VISIT_US",
            **patch,
        },
    )
    reason = validate_fact_api_action_body("address", candidate)
    if reason:
        raise ValueError(f"The reviewed address is not valid: {_plain_action_reason(reason)}")
    return {
        field: candidate.get(field) if isinstance(candidate.get(field), str) else None
        for field in _ADDRESS_PATCH_FIELDS
    }


def _planning_blockers(
    item: dict[str, Any],
    linked_actions: list[dict[str, Any]],
    submission: dict[str, Any] | None,
) -> list[dict[str, str]]:
    blockers = []
    for action in linked_actions:
        reason = action.get("reason")
        if not reason:
            continue
        for part in str(reason).split(";"):
            message = _plain_action_reason(part.strip())
            if message:
                blockers.append(
                    {
                        "message": message,
                        "technical_detail": str(reason),
                    }
                )
    if linked_actions or blockers:
        return _unique_blockers(blockers)

    issues = submission.get("issues", []) if isinstance(submission, dict) else []
    field = str(item.get("field") or "")
    court_issues = [
        issue
        for issue in issues
        if isinstance(issue, dict)
        and (
            issue.get("code") == "COURT_SLUG_NOT_FOUND"
            or "DUPLICATE" in str(issue.get("code") or "")
        )
    ]
    relevant = court_issues or [
        issue
        for issue in issues
        if isinstance(issue, dict)
        and (
            _field_paths_overlap(field, str(issue.get("field") or ""))
            or issue.get("severity") == "error"
        )
    ]
    for issue in relevant:
        blockers.append(
            {
                "message": _plain_issue_message(issue),
                "technical_detail": (
                    f"{issue.get('code') or 'SOURCE_ISSUE'}: "
                    f"{issue.get('message') or 'No diagnostic message was recorded'}"
                ),
            }
        )
    if blockers:
        return _unique_blockers(blockers)
    section = _section_label_for_field(field)
    return [
        {
            "message": f"There was not enough submitted information to create the {section} section.",
            "technical_detail": "No complete, non-empty request body was planned for this field.",
        }
    ]


def _court_target_resolution_payload(
    submission: dict[str, Any] | None,
    override: CourtTargetOverride | None,
) -> dict[str, Any] | None:
    if not isinstance(submission, dict):
        return None
    issue = next(
        (
            value
            for value in submission.get("issues", [])
            if isinstance(value, dict) and value.get("code") == "COURT_SLUG_NOT_FOUND"
        ),
        None,
    )
    if issue is None and override is None:
        return None
    suggestion = issue.get("cleaned_value") if isinstance(issue, dict) else None
    return {
        "source_row_number": submission.get("source", {}).get("source_row_number"),
        "submitted_slug": str(
            (issue or {}).get("raw_value") or submission.get("court_slug") or ""
        ),
        "suggestion": suggestion if isinstance(suggestion, dict) else None,
        "override": override.model_dump(mode="json") if override else None,
    }


def _plain_issue_message(issue: dict[str, Any]) -> str:
    code = str(issue.get("code") or "")
    field = _human_field_name(str(issue.get("field") or "this value"))
    if code == "COURT_SLUG_NOT_FOUND":
        return "This court could not be found in the FaCT database."
    if "DUPLICATE" in code:
        return "A duplicate court submission needs an authoritative source row to be selected."
    if code.startswith("ADDRESS_OS_"):
        return "The address could not be matched safely against Ordnance Survey data."
    if code.startswith("INVALID_") or issue.get("severity") == "error":
        detail = str(issue.get("message") or "The submitted value is invalid").rstrip(".")
        return f"The submitted {field} cannot be used: {detail}."
    message = str(issue.get("message") or "This submitted value needs review").rstrip(".")
    return f"The submitted {field} needs attention: {message}."


def _plain_action_reason(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().rstrip(".")
    replacements = {
        "courtId": "court identifier",
        "addressLine1": "address line 1",
        "townCity": "town or city",
        "addressType": "address type",
        "liftDoorWidth": "lift door width",
        "liftDoorLimit": "lift weight limit",
        "liftSupportPhoneNumber": "lift support phone number",
        "accessibleEntrancePhoneNumber": "accessible entrance phone number",
    }
    for internal, friendly in replacements.items():
        text = text.replace(internal, friendly)
    if "required by the FaCT API" in text:
        return f"The proposed section is missing information required by FaCT: {text}."
    return f"The proposed section cannot be sent yet: {text}."


def _human_field_name(field: str) -> str:
    value = re.sub(r"\[\d+\]", "", field).replace("_", " ").replace(".", " ")
    return re.sub(r"\s+", " ", value).strip()


def _field_paths_overlap(left: str, right: str) -> bool:
    return bool(left and right) and (
        left == right
        or left.startswith(right + ".")
        or left.startswith(right + "[")
        or right.startswith(left + ".")
        or right.startswith(left + "[")
    )


def _section_label_for_field(field: str) -> str:
    if field == "facilities.accessible_toilet_description" or field.startswith(
        "facilities.accessible_"
    ):
        return "accessibility options"
    root = field.split("[", 1)[0].split(".", 1)[0]
    return {
        "addresses": "address",
        "contacts": "contact details",
        "opening_hours": "opening hours",
        "facilities": "facilities",
        "counter_service": "counter service",
    }.get(root, "relevant FaCT")


def _unique_blockers(values: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result = []
    for value in values:
        message = value["message"]
        if message in seen:
            continue
        seen.add(message)
        result.append(value)
    return result


def _address_os_preflight_result(
    action: dict[str, Any],
    body: dict[str, Any],
    lookup: Callable[[str], ApiResponse],
    *,
    approved_address: dict[str, Any] | None = None,
) -> AddressPreflightResult:
    """Check an address only when this immutable run has no verified evidence."""

    if action.get("resource") != "address":
        return AddressPreflightResult("ready")
    evidence = action.get("address_verification")
    if isinstance(evidence, dict) and evidence.get("status") in {"auto_normalised", "verified"}:
        return AddressPreflightResult("ready")
    postcode = body.get("postcode")
    if not isinstance(postcode, str) or not postcode:
        return AddressPreflightResult("ready")
    try:
        response = lookup(postcode)
    except (httpx.HTTPError, RuntimeError) as exc:
        _, reason = _preflight_error_details("address postcode verification", exc)
        return AddressPreflightResult("unknown", reason=reason)
    if response.status_code == 200 and approved_address:
        uprn = approved_address.get("model_result", {}).get("uprn")
        if isinstance(uprn, str) and _response_contains_uprn(response.body, uprn):
            return AddressPreflightResult("ready", http_status=200)
        return AddressPreflightResult(
            "blocked",
            http_status=200,
            reason=(
                "The approved Ordnance Survey candidate is no longer returned for "
                "this postcode; review the address again before writing"
            ),
        )
    if response.status_code == 200:
        return AddressPreflightResult("ready", http_status=200)
    if response.status_code in {400, 404}:
        messages = (
            _validation_error_messages(response.body) if isinstance(response.body, dict) else []
        )
        detail = "; ".join(messages) if messages else "postcode was rejected"
        return AddressPreflightResult(
            "blocked",
            http_status=response.status_code,
            reason=(
                "Address cannot be sent because the FaCT/Ordnance Survey postcode lookup failed: "
                f"{detail}"
            ),
        )
    if response.status_code == 429:
        return AddressPreflightResult(
            "unknown",
            http_status=429,
            reason="FaCT/Ordnance Survey rate-limited address verification (HTTP 429). Try again later.",
        )
    return AddressPreflightResult(
        "unknown",
        http_status=response.status_code,
        reason=(
            "FaCT/Ordnance Survey address verification returned an unexpected response "
            f"(HTTP {response.status_code}). Try the preflight again."
        ),
    )


def _response_contains_uprn(body: Any, uprn: str) -> bool:
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        return False
    for result in body["results"]:
        if not isinstance(result, dict):
            continue
        dpa = result.get("DPA") or result.get("dpa")
        if not isinstance(dpa, dict):
            continue
        candidate_uprn = dpa.get("UPRN") or dpa.get("uprn")
        if candidate_uprn is not None and str(candidate_uprn).strip() == uprn:
            return True
    return False


def _requires_manual_text_review(item: dict[str, Any]) -> bool:
    """Keep overlength-source explanation repairs out of automatic/bulk approval."""

    if item.get("kind") != "field" or not str(item.get("field") or "").endswith(
        ".explanation"
    ):
        return False
    llm_input = item.get("llm_input")
    source = llm_input.get("cleaned_value") if isinstance(llm_input, dict) else None
    return isinstance(source, str) and len(source) > 250


def _is_optional_explanation_review(item: dict[str, Any]) -> bool:
    return item.get("kind") == "field" and str(item.get("field") or "").endswith(
        ".explanation"
    )


def _legacy_address_reason_is_only_blocker(action: dict[str, Any]) -> bool:
    evidence = action.get("address_verification")
    if not isinstance(evidence, dict) or evidence.get("status") != "review_required":
        return False
    expected = f"Address verification requires review: {evidence.get('message') or ''}"
    return str(action.get("reason") or "").strip() == expected.strip()


def _legacy_lift_request_defaults(
    action: dict[str, Any], submission: dict[str, Any] | None
) -> dict[str, int]:
    """Recover approved blank lift defaults for reports created before the policy."""

    if action.get("resource") != "accessibility_options" or not isinstance(
        submission, dict
    ):
        return {}
    body = action.get("body")
    if not isinstance(body, dict) or body.get("lift") is not True:
        return {}
    facilities = submission.get("facilities")
    if not isinstance(facilities, dict):
        cleaned = submission.get("cleaned")
        facilities = cleaned.get("facilities") if isinstance(cleaned, dict) else None
    if not isinstance(facilities, dict) or facilities.get("lift_available") is not True:
        return {}

    defaults = {}
    for api_field, source_field in (
        ("liftDoorWidth", "lift_door_width"),
        ("liftDoorLimit", "lift_weight_limit"),
    ):
        source_value = facilities.get(source_field)
        if body.get(api_field) is None and is_unavailable_lift_measurement(source_value):
            defaults[api_field] = 1
    return defaults


def _legacy_lift_reason_is_only_blocker(
    action: dict[str, Any], request_defaults: dict[str, int]
) -> bool:
    if not request_defaults:
        return False
    required_field_by_reason = {
        "liftDoorWidth is required by the FaCT API when lift is true": "liftDoorWidth",
        "liftDoorLimit is required by the FaCT API when lift is true": "liftDoorLimit",
    }
    reasons = {
        reason.strip()
        for reason in str(action.get("reason") or "").split(";")
        if reason.strip()
    }
    return bool(reasons) and all(
        required_field_by_reason.get(reason) in request_defaults for reason in reasons
    )


def _reason_contains_only_duplicate_type_conflicts(reason: str) -> bool:
    reasons = [item.strip() for item in reason.split(";") if item.strip()]
    return bool(reasons) and all(
        item.startswith("Multiple ") and " entries use business type " in item
        for item in reasons
    )


def _comparison_operation_validation_reason(
    action: dict[str, Any], comparison: TargetComparison, court_id: str
) -> str | None:
    resource = str(action.get("resource") or "")
    for operation in comparison.operations:
        body = dict(operation.get("body") or {})
        if resource != "professional_information":
            body["courtId"] = court_id
        body = normalise_fact_api_action_body(resource, body)
        reason = validate_fact_api_action_body(resource, body)
        if reason:
            return reason
    return None


def _preflight_error_details(operation: str, exc: Exception) -> tuple[int | None, str]:
    """Describe safe, actionable transport failures without persisting response bodies."""

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in {401, 403}:
            return (
                status_code,
                (
                    f"FaCT API rejected the {operation} (HTTP {status_code}). "
                    "Refresh FACT_DATA_API_BEARER_TOKEN and restart the importer UI."
                ),
            )
        return status_code, f"FaCT API rejected the {operation} (HTTP {status_code})."

    if isinstance(exc, httpx.ConnectError):
        return (
            None,
            (
                f"Could not connect to FaCT API for {operation}. "
                "Confirm the FaCT Data API application is running at FACT_DATA_API_BASE_URL."
            ),
        )

    if isinstance(exc, httpx.TimeoutException):
        return None, f"FaCT API timed out during {operation}. Try the check again."

    if isinstance(exc, ValueError):
        return None, f"FaCT API returned an invalid response during {operation}: {exc}"

    return None, f"FaCT API request failed during {operation} ({type(exc).__name__})."
