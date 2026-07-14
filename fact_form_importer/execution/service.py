"""Conservative, one-court execution service for archived API action reports."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
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
from fact_form_importer.execution.report import (
    EXECUTION_SUMMARY_VERSION,
    build_execution_summary,
)
from fact_form_importer.llm.review import load_or_derive_llm_actions_review
from fact_form_importer.output.archive import load_run_archive
from fact_form_importer.output.fact_api_manifest import (
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
        self._llm_review_cache: dict[str, dict[str, Any]] = {}
        self._client = client
        self._postcode_lookup = RateLimitedPostcodeLookup(
            _unconfigured_postcode_lookup,
            min_interval_seconds=self.config.os_address_min_interval_seconds,
        )

    def get_ledger(self, run_id: str) -> ExecutionLedger:
        return self.store.load(run_id)

    def get_llm_actions_review(self, run_id: str) -> dict[str, Any]:
        report = self._llm_review_report(run_id)
        readiness = self._readiness_report(run_id)
        approvals, added = self.approval_store.reconcile_address_policy(run_id, report, readiness)
        if added:
            self._save_execution_summary(run_id, approvals=approvals, review_report=report)
        execution = self.store.load(run_id)
        actions_by_id = {
            str(action.get("action_id")): (str(record.get("court_slug") or ""), action)
            for record in readiness.get("records", [])
            for action in record.get("actions", [])
            if action.get("action_id")
        }
        items = []
        for value in report.get("items", []):
            item = dict(value)
            approved = item.get("review_id") in approvals.approvals
            approval = approvals.approvals.get(str(item.get("review_id") or ""))
            item["approved_at"] = approval.approved_at if approval is not None else None
            item["approval_method"] = approval.approval_method if approval else None
            item["approval_policy_version"] = approval.policy_version if approval else None
            item["approval_rationale"] = approval.rationale if approval else None
            linked_actions = []
            for action_id in item.get("dependent_action_ids", []):
                linked = actions_by_id.get(str(action_id))
                if linked is None:
                    continue
                court_slug, action = linked
                linked_actions.append(
                    {
                        "action_id": action_id,
                        "court_slug": court_slug,
                        "resource": action.get("resource"),
                        "status": self._effective_action_status(
                            run_id, court_slug, action, execution, approvals, report
                        ),
                    }
                )
            item["dependent_actions"] = linked_actions
            already_executed = bool(linked_actions) and all(
                action["status"] == "succeeded" for action in linked_actions
            )
            item["approval_status"] = (
                "approved"
                if approved
                else "already_executed"
                if item.get("actionable") and already_executed
                else "pending"
                if item.get("actionable")
                else "not_actionable"
            )
            items.append(item)
        return {**report, "items": items, "approval_counts": _approval_counts(items)}

    def reconcile_automatic_approvals(self, run_id: str) -> LlmApprovalLedger:
        """Apply the strict address policy without executing any FaCT action."""

        report = self._llm_review_report(run_id)
        approvals, added = self.approval_store.reconcile_address_policy(
            run_id, report, self._readiness_report(run_id)
        )
        if added:
            self._save_execution_summary(run_id, approvals=approvals, review_report=report)
        return approvals

    def approve_llm_review(self, run_id: str, review_id: str) -> LlmApprovalLedger:
        report = self._llm_review_report(run_id)
        item = next(
            (value for value in report.get("items", []) if value.get("review_id") == review_id),
            None,
        )
        if item is None:
            raise ValueError(f"LLM review item '{review_id}' does not exist in run '{run_id}'")
        if not item.get("actionable") or item.get("outcome") != "accepted":
            raise ValueError("This LLM result is read-only and cannot be approved")
        current_item = next(
            (
                value
                for value in self.get_llm_actions_review(run_id).get("items", [])
                if value.get("review_id") == review_id
            ),
            None,
        )
        if current_item and current_item.get("approval_status") == "already_executed":
            raise ValueError("This LLM result was already used by a succeeded API action")
        ledger = self.approval_store.approve(run_id, review_id)
        self._save_execution_summary(run_id, approvals=ledger, review_report=report)
        return ledger

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
        client, close = self._client_or_new()
        try:
            self._preflight_actions(run_id, ledger, record, record.get("actions", []), client)
            for action in record.get("actions", []):
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
                actions = self._batch_actions_to_attempt(ledger, record)
                if not actions:
                    self._update_court_status(court, record.get("actions", []))
                    self._save_with_summary(run_id, ledger, report)
                    continue
                try:
                    self._preflight_actions(run_id, ledger, record, actions, client)
                    for action in actions:
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
        approvals, added = self.approval_store.reconcile_address_policy(run_id, report, readiness)
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

        return json.loads(report_path.read_text(encoding="utf-8"))

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
        if planned_id and planned_id != court.court_id:
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
        approvals, _ = self.approval_store.reconcile_address_policy(
            run_id, review_report, self._readiness_report(run_id)
        )
        for action in actions:
            state = self._action_state(ledger, court_slug, action["action_id"])
            if state.status == "succeeded":
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
            body = self._execution_body(
                run_id,
                action,
                court.court_id,
                approved_address=approved_address,
            )
            body_reason = validate_fact_api_action_body(str(action.get("resource") or ""), body)
            if body_reason:
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "preflight",
                    None,
                    f"Action body cannot be sent to FaCT: {body_reason}",
                )
                continue
            address_result = _address_os_preflight_result(
                action,
                body,
                self._postcode_lookup.get,
                approved_address=approved_address,
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
                continue
            try:
                target = client.get(_preflight_path(str(action["path"])))
            except httpx.HTTPError as exc:
                http_status, reason = _preflight_error_details("target section check", exc)
                self._set_action(
                    ledger, court_slug, action, "unknown", "preflight", http_status, reason
                )
                continue
            if _target_has_existing_data(target.status_code, target.body):
                self._set_action(
                    ledger,
                    court_slug,
                    action,
                    "blocked",
                    "preflight",
                    target.status_code,
                    "Target section already contains FaCT data; retained for human review",
                )
            elif target.status_code in {200, 204, 404}:
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
        self._update_court_status(court_state, record.get("actions", []))

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
        self._update_court_status(self._court_state(ledger, court_slug), record.get("actions", []))

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
            approvals, _ = self.approval_store.reconcile_address_policy(
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

    @staticmethod
    def _action_review_ids(action: dict[str, Any], review_report: dict[str, Any]) -> list[str]:
        action_id = str(action.get("action_id") or "")
        explicit = {str(review_id) for review_id in action.get("llm_review_ids", []) if review_id}
        derived = {
            str(item["review_id"])
            for item in review_report.get("items", [])
            if item.get("actionable")
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
        return next(
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

    def _effective_action_status(
        self,
        run_id: str,
        court_slug: str,
        action: dict[str, Any],
        ledger: ExecutionLedger,
        approvals: LlmApprovalLedger,
        review_report: dict[str, Any],
    ) -> str:
        del run_id  # retained in the signature for a stable service-facing helper
        court = ledger.courts.get(court_slug)
        state = court.actions.get(str(action.get("action_id"))) if court else None
        status = state.status if state else "planned"
        if status in {"blocked", "failed", "unknown", "running", "succeeded"}:
            return status
        review_ids = self._action_review_ids(action, review_report)
        if any(review_id not in approvals.approvals for review_id in review_ids):
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
    return {
        "total": len(actionable),
        "approved": len(approved),
        "manual_approved": sum(item.get("approval_method") == "manual" for item in approved),
        "auto_approved": sum(item.get("approval_method") == "policy" for item in approved),
        "pending": sum(item.get("approval_status") == "pending" for item in actionable),
        "already_executed": sum(
            item.get("approval_status") == "already_executed" for item in actionable
        ),
        "not_actionable": sum(not item.get("actionable") for item in items),
    }


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
