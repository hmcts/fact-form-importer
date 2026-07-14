"""Conservative, one-court execution service for archived API action reports."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx

from fact_form_importer.config import AppConfig
from fact_form_importer.execution.approvals import LlmApprovalLedger, LlmApprovalStore
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
    ExecutionReviewLedger,
    ExecutionReviewStore,
    TargetComparison,
    build_target_comparison,
    target_change_id,
)
from fact_form_importer.llm.review import load_or_derive_llm_actions_review
from fact_form_importer.output.archive import load_run_archive
from fact_form_importer.output.fact_api_manifest import (
    API_MANIFEST_VERSION,
    normalise_fact_api_action_body,
    validate_fact_api_action_body,
)
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
        self._client = client
        self._postcode_lookup = RateLimitedPostcodeLookup(
            _unconfigured_postcode_lookup,
            min_interval_seconds=self.config.os_address_min_interval_seconds,
        )

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
            if response.status_code not in {200, 204, 404}:
                raise ValueError(
                    f"Target section comparison returned HTTP {response.status_code}"
                )
            comparison = build_target_comparison(court_slug, effective_action, response.body)
            self.review_store.save_comparison(run_id, comparison)
            self._save_execution_summary(run_id)
            return comparison
        finally:
            if close:
                client.close()

    def refresh_all_target_comparisons(self, run_id: str) -> None:
        report = self._readiness_report(run_id)
        failures: list[str] = []
        for record in report.get("records", []):
            court_slug = str(record.get("court_slug") or "")
            try:
                actions = self._active_actions(run_id, record)
                if any(action.get("source_selection_required") for action in actions):
                    selection = self.review_store.load(run_id).source_selections.get(court_slug)
                    if selection is None:
                        continue
                for action in actions:
                    self.refresh_target_comparison(
                        run_id, court_slug, str(action.get("action_id") or "")
                    )
            except (ValueError, httpx.HTTPError) as exc:
                failures.append(f"{court_slug}: {type(exc).__name__}")
        self._save_execution_summary(run_id)
        if failures:
            raise ValueError(
                f"FaCT comparison scan completed with {len(failures)} court error(s)"
            )

    def approve_target_change(self, run_id: str, change_id: str) -> ExecutionReviewLedger:
        self._readiness_report(run_id)
        ledger = self.review_store.approve_target(run_id, change_id)
        self._save_execution_summary(run_id)
        return ledger

    def get_api_changes_review(self, run_id: str) -> dict[str, Any]:
        report = self._readiness_report(run_id)
        review = self.review_store.load(run_id)
        execution = self.store.load(run_id)
        approval_ledger = self.approval_store.load(run_id)
        llm_report = self._llm_review_report(run_id)
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
                    comparison_payload["differences"] = _component_differences(
                        comparison.current, comparison.proposed
                    )
                review_ids = self._action_review_ids(action, llm_report)
                pending_value_holds = [
                    {
                        "review_id": review_id,
                        "kind": "llm",
                    }
                    for review_id in review_ids
                    if review_id not in approval_ledger.approvals
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
                        "execution_status": self._effective_action_status(
                            run_id,
                            court_slug,
                            action,
                            execution,
                            approval_ledger,
                            llm_report,
                        ),
                    }
                )
        return {"run_id": run_id, "changes": changes}

    def get_llm_actions_review(self, run_id: str) -> dict[str, Any]:
        report = self._llm_review_report(run_id)
        readiness = self._readiness_report(run_id)
        approvals, added = self.approval_store.reconcile_policies(run_id, report, readiness)
        if added:
            self._save_execution_summary(run_id, approvals=approvals, review_report=report)
        execution = self.store.load(run_id)
        execution_review = self.review_store.load(run_id)
        submissions_by_row = self._submissions_by_row(run_id)
        actions_by_id = {
            str(action.get("action_id")): (str(record.get("court_slug") or ""), action)
            for record in readiness.get("records", [])
            for action in record.get("actions", [])
            if action.get("action_id")
        }
        actions_by_review_id: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for court_slug, action in actions_by_id.values():
            for review_id in action.get("llm_review_ids", []):
                actions_by_review_id.setdefault(str(review_id), []).append(
                    (court_slug, action)
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
            item["approved_at"] = approval.approved_at if approval is not None else None
            item["approval_method"] = approval.approval_method if approval else None
            item["approval_policy_version"] = approval.policy_version if approval else None
            item["approval_rationale"] = approval.rationale if approval else None
            item["approved_address_patch"] = (
                approval.approved_address_patch if approval else None
            )
            item["decision_history"] = (
                [decision.model_dump(mode="json") for decision in approval.decision_history]
                if approval
                else []
            )
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
                            run_id, court_slug, action, execution, approvals, report
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
            if "approvable" not in item:
                item["approvable"] = _is_usable_review_item(item)
            already_executed = bool(linked_actions) and all(
                action["status"] == "succeeded" for action in linked_actions
            )
            item["approval_status"] = (
                "approved"
                if approved
                else "already_executed"
                if item.get("actionable") and already_executed
                else "pending"
                if item.get("approvable", item.get("actionable"))
                else "not_actionable"
            )
            item["comparison_summary"] = _llm_item_comparison(item)
            items.append(item)
        return {**report, "items": items, "approval_counts": _approval_counts(items)}

    def reconcile_automatic_approvals(self, run_id: str) -> LlmApprovalLedger:
        """Apply the strict address policy without executing any FaCT action."""

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
        else:
            ledger = self.approval_store.approve(run_id, review_id)
        self._save_execution_summary(run_id, approvals=ledger, review_report=report)
        return ledger

    def _submissions_by_row(self, run_id: str) -> dict[int, dict[str, Any]]:
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
        return {
            int(row): submission
            for submission in submissions
            if isinstance(submission, dict)
            and isinstance(
                row := submission.get("source", {}).get("source_row_number"), int
            )
        }

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

    def execute_all_safe_actions(self, run_id: str) -> ExecutionLedger:
        """Execute every unattempted, preflight-safe court action sequentially.

        This is intentionally a single-threaded operation. It shares the
        postcode cache/rate limiter, preserves progress after each court, and
        never automatically retries terminal action states from an earlier
        execution attempt.
        """

        self._require_writes_enabled()
        report = self._readiness_report(run_id)
        records = sorted(
            report.get("records", []), key=lambda record: str(record.get("court_slug") or "")
        )
        ledger = self.store.load(run_id)
        client, close = self._client_or_new()
        try:
            for record in records:
                court_slug = str(record.get("court_slug") or "")
                court = self._court_state(ledger, court_slug, record.get("court_id"))
                active_actions = self._active_actions(run_id, record)
                actions = self._batch_actions_to_attempt(
                    ledger, {**record, "actions": active_actions}
                )
                if not actions:
                    self._update_court_status(court, record.get("actions", []))
                    self._save_with_summary(run_id, ledger, report)
                    continue
                try:
                    for action in actions:
                        # Re-read and hash-check immediately before each write.
                        self._preflight_actions(run_id, ledger, record, [action], client)
                        state = self._action_state(ledger, court_slug, str(action["action_id"]))
                        if state.status == "ready":
                            self._write_action(run_id, ledger, record, action, client)
                except Exception as exc:  # retain progress and continue with later courts
                    self._record_unexpected_court_error(ledger, record, actions, exc)
                self._save_with_summary(run_id, ledger, report)
        finally:
            if close:
                client.close()
        return self._save_with_summary(run_id, ledger, report)

    def get_execution_summary(self, run_id: str) -> dict[str, Any]:
        report = self._llm_review_report(run_id)
        readiness = self._readiness_report(run_id)
        approvals, added = self.approval_store.reconcile_policies(run_id, report, readiness)
        existing = self.store.load_summary(run_id)
        if (
            not added
            and existing is not None
            and existing.get("summary_version") == EXECUTION_SUMMARY_VERSION
        ):
            return existing
        summary = build_execution_summary(
            run_id,
            readiness,
            self.store.load(run_id),
            review_report=report,
            approvals=approvals,
            execution_review=self.review_store.load(run_id),
        )
        return self.store.save_summary(run_id, summary)

    def _record(self, run_id: str, court_slug: str) -> dict[str, Any]:
        report = self._readiness_report(run_id)
        record = next(
            (item for item in report.get("records", []) if item.get("court_slug") == court_slug),
            None,
        )
        if record is None:
            raise ValueError(f"Court '{court_slug}' is not in the API readiness report")
        return record

    def _readiness_report(self, run_id: str) -> dict[str, Any]:
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
        if (
            latest.get("run_id") == run_id
            and str(report.get("manifest_version") or "") < API_MANIFEST_VERSION
            and (archive["path"] / "submissions_cleaned.json").exists()
        ):
            return derive_latest_execution_overlay(
                run_id,
                archive["path"],
                self.output_root,
                report,
                succeeded_action_ids,
            )
        return report

    def _llm_review_report(self, run_id: str) -> dict[str, Any]:
        if run_id in self._llm_review_cache:
            return self._llm_review_cache[run_id]
        archive = load_run_archive(self.output_root, run_id)
        if archive is None:
            raise ValueError(f"Run '{run_id}' does not exist")
        report = load_or_derive_llm_actions_review(archive["path"])
        self._llm_review_cache[run_id] = report
        return report

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
        )
        return self.store.save_summary(run_id, summary)

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
        self._postcode_lookup.set_lookup(
            lambda postcode: client.get(f"/search/address/v1/postcode/{quote(postcode, safe='')}")
        )
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
            if action.get("readiness") != "ready" and not (
                approved_address and _legacy_address_reason_is_only_blocker(action)
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
            body_reasons = [
                validate_fact_api_action_body(str(action.get("resource") or ""), body)
                for body in bodies
            ]
            body_reason = next((reason for reason in body_reasons if reason), None)
            if body_reason or not bodies:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "preflight",
                    None,
                    f"Action body cannot be sent to FaCT: {body_reason or 'empty section'}",
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
            if comparison and comparison.is_no_change:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "succeeded",
                    "preflight",
                    target.status_code,
                    "FaCT already contains the reviewed proposed section; no write was required",
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
                        "Existing FaCT section replacement requires approval of the displayed before and after values",
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
        elif action.get("resource") in {
            "address",
            "contact_detail",
            "court_opening_hours",
        }:
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
                                f"Replacement operation cannot be sent to FaCT: {body_reason}",
                            )
                            break
                    try:
                        response = client.write(method, str(operation.get("path") or ""), body)
                    except httpx.TimeoutException:
                        self._set_action(
                            ledger,
                            court_slug,
                            action,
                            "unknown",
                            "execute",
                            None,
                            "Section replacement timed out; outcome is unknown and surplus entries were not deleted",
                        )
                        break
                    except httpx.HTTPError as exc:
                        http_status, reason = _preflight_error_details(
                            "section replacement request", exc
                        )
                        self._set_action(
                            ledger, court_slug, action, "failed", "execute", http_status, reason
                        )
                        break
                    if not 200 <= response.status_code < 300:
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
                    completed_operations += 1
                    self._save_with_summary(run_id, ledger)
                else:
                    self._set_action(
                        ledger,
                        court_slug,
                        action,
                        "succeeded",
                        "execute",
                        200,
                        f"Completed {completed_operations} reviewed section replacement operation(s)",
                    )
                if state.status in {"blocked", "failed", "unknown"}:
                    self._capture_partial_section_state(
                        run_id,
                        ledger,
                        court_slug,
                        action,
                        comparison,
                        completed_operations,
                        client,
                    )
        else:
            body = self._execution_body(run_id, action, court_id)
            body_reason = validate_fact_api_action_body(str(action.get("resource") or ""), body)
            if body_reason:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "execute",
                    None,
                    f"Action body cannot be sent to FaCT: {body_reason}",
                )
            else:
                try:
                    response = client.write(action["method"], action["path"], body)
                except httpx.TimeoutException:
                    self._set_action(
                        ledger,
                        court_slug,
                        action,
                        "unknown",
                        "execute",
                        None,
                        "Write timed out; outcome is unknown",
                    )
                except httpx.HTTPError as exc:
                    http_status, reason = _preflight_error_details("write request", exc)
                    self._set_action(
                        ledger, court_slug, action, "failed", "execute", http_status, reason
                    )
                else:
                    if 200 <= response.status_code < 300:
                        self._set_action(
                            ledger,
                            court_slug,
                            action,
                            "succeeded",
                            "execute",
                            response.status_code,
                            None,
                        )
                    else:
                        self._set_action(
                            ledger,
                            court_slug,
                            action,
                            "failed",
                            "execute",
                            response.status_code,
                            _write_rejection_reason(response.status_code, response.body),
                        )
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
            note = "Live FaCT could not be re-read after the partial replacement."
        else:
            if response.status_code in {200, 204, 404}:
                effective_action = {
                    **action,
                    "proposed_items": comparison.proposed,
                }
                refreshed = build_target_comparison(
                    court_slug, effective_action, response.body
                )
                self.review_store.save_comparison(run_id, refreshed)
                note = (
                    "Live FaCT was re-read after the partial replacement; "
                    "the replacement approval was invalidated for a new review."
                )
            else:
                note = (
                    "Live FaCT returned an unexpected response when re-read after "
                    "the partial replacement."
                )
        state.reason = f"{state.reason or 'Section replacement stopped.'} {note}"
        state.attempts.append(
            ActionAttempt(
                operation="preflight",
                outcome=state.status,
                http_status=None,
                message=f"{completed_operations} replacement operation(s) completed. {note}",
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
        for item in items:
            if action.get("resource") != "professional_information":
                item["courtId"] = court_id
        return [
            normalise_fact_api_action_body(str(action.get("resource") or ""), item)
            for item in items
        ]

    def _active_actions(
        self, run_id: str, record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        actions = list(record.get("actions", []))
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
    ) -> str:
        court = ledger.courts.get(court_slug)
        state = court.actions.get(str(action.get("action_id"))) if court else None
        status = state.status if state else "planned"
        if status in {"blocked", "failed", "unknown", "running", "succeeded"}:
            return status
        review_state = self.review_store.load(run_id)
        if action.get("source_selection_required"):
            selection = review_state.source_selections.get(court_slug)
            if selection is None:
                return "awaiting_approval"
            if selection.source_row_number != action.get("source_row_number"):
                return "not_selected"
        review_ids = self._action_review_ids(action, review_report)
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
            item.get("approval_policy_version") == "high-single-os-candidate-v1"
            for item in policy_approved
        ),
        "auto_approved_unchanged_fields": sum(
            item.get("approval_policy_version") == "high-unchanged-field-v1"
            for item in policy_approved
        ),
        "pending": sum(item.get("approval_status") == "pending" for item in actionable),
        "already_executed": sum(
            item.get("approval_status") == "already_executed" for item in actionable
        ),
        "not_actionable": sum(not item.get("actionable") for item in items),
        "reviewable_total": len(reviewable),
        "review_pending": sum(
            item.get("approval_status") == "pending" for item in reviewable
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


def _legacy_address_reason_is_only_blocker(action: dict[str, Any]) -> bool:
    evidence = action.get("address_verification")
    if not isinstance(evidence, dict) or evidence.get("status") != "review_required":
        return False
    expected = f"Address verification requires review: {evidence.get('message') or ''}"
    return str(action.get("reason") or "").strip() == expected.strip()


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
