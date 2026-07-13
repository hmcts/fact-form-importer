"""Conservative, one-court execution service for reviewed API action reports."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from fact_form_importer.config import AppConfig
from fact_form_importer.execution.fact_api import FactApiExecutionClient
from fact_form_importer.execution.ledger import ExecutionLedgerStore
from fact_form_importer.execution.models import (
    ActionAttempt,
    ActionExecutionState,
    CourtExecutionState,
    ExecutionLedger,
    utc_now,
)
from fact_form_importer.output.archive import load_run_archive
from fact_form_importer.output.fact_api_manifest import validate_fact_api_action_body


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
        self._client = client

    def get_ledger(self, run_id: str) -> ExecutionLedger:
        return self.store.load(run_id)

    def check_court(self, run_id: str, court_slug: str) -> ExecutionLedger:
        record = self._record(run_id, court_slug)
        ledger = self.store.load(run_id)
        client, close = self._client_or_new()
        try:
            self._preflight_actions(ledger, record, record.get("actions", []), client)
        finally:
            if close:
                client.close()
        return self.store.save(ledger)

    def execute_action(self, run_id: str, court_slug: str, action_id: str) -> ExecutionLedger:
        self._require_writes_enabled()
        record = self._record(run_id, court_slug)
        action = next((item for item in record.get("actions", []) if item.get("action_id") == action_id), None)
        if action is None:
            raise ValueError(f"Action '{action_id}' is not in court '{court_slug}'")
        ledger = self.store.load(run_id)
        client, close = self._client_or_new()
        try:
            self._preflight_actions(ledger, record, [action], client)
            state = self._action_state(ledger, court_slug, action_id)
            if state.status != "ready":
                return self.store.save(ledger)
            self._write_action(ledger, record, action, client)
        finally:
            if close:
                client.close()
        return self.store.save(ledger)

    def execute_safe_court_actions(self, run_id: str, court_slug: str) -> ExecutionLedger:
        self._require_writes_enabled()
        record = self._record(run_id, court_slug)
        ledger = self.store.load(run_id)
        client, close = self._client_or_new()
        try:
            self._preflight_actions(ledger, record, record.get("actions", []), client)
            for action in record.get("actions", []):
                state = self._action_state(ledger, court_slug, action["action_id"])
                if state.status == "ready":
                    self._write_action(ledger, record, action, client)
        finally:
            if close:
                client.close()
        return self.store.save(ledger)

    def _record(self, run_id: str, court_slug: str) -> dict[str, Any]:
        archive = load_run_archive(self.output_root, run_id)
        if archive is None:
            raise ValueError(f"Run '{run_id}' does not exist")
        report_path = archive["path"] / "api_readiness_report.json"
        if not report_path.exists():
            raise ValueError("This archive does not contain an API readiness report")
        import json

        report = json.loads(report_path.read_text(encoding="utf-8"))
        record = next((item for item in report.get("records", []) if item.get("court_slug") == court_slug), None)
        if record is None:
            raise ValueError(f"Court '{court_slug}' is not in the API readiness report")
        return record

    def _client_or_new(self) -> tuple[FactApiExecutionClient, bool]:
        return (self._client, False) if self._client else (FactApiExecutionClient(self.config), True)

    def _require_writes_enabled(self) -> None:
        if not self.config.fact_data_api_writes_enabled:
            raise ValueError("FaCT API writes are disabled by FACT_DATA_API_WRITES_ENABLED")

    def _preflight_actions(
        self,
        ledger: ExecutionLedger,
        record: dict[str, Any],
        actions: Iterable[dict[str, Any]],
        client: FactApiExecutionClient,
    ) -> None:
        court_slug = str(record["court_slug"])
        try:
            court = client.lookup_court(court_slug)
        except (httpx.HTTPError, ValueError) as exc:
            for action in actions:
                self._set_action(
                    ledger, court_slug, action, "unknown", "preflight", None,
                    f"Court lookup could not be completed ({type(exc).__name__})",
                )
            return
        if court is None:
            for action in actions:
                self._set_action(ledger, court_slug, action, "blocked", "preflight", 404, "Court does not exist in FaCT")
            return
        planned_id = record.get("court_id")
        if planned_id and planned_id != court.court_id:
            for action in actions:
                self._set_action(
                    ledger, court_slug, action, "blocked", "preflight", None,
                    "Court UUID no longer matches the reviewed report",
                )
            return
        court_state = self._court_state(ledger, court_slug, court.court_id)
        for action in actions:
            if action.get("readiness") != "ready":
                self._set_action(
                    ledger, court_slug, action, "blocked", "preflight", None,
                    str(action.get("reason") or "Action body is not ready for FaCT"),
                )
                continue
            state = self._action_state(ledger, court_slug, action["action_id"])
            if state.status == "succeeded":
                continue
            body_reason = validate_fact_api_action_body(
                str(action.get("resource") or ""), self._execution_body(action, court.court_id)
            )
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
            try:
                target = client.get(_preflight_path(str(action["path"])))
            except httpx.HTTPError as exc:
                self._set_action(
                    ledger, court_slug, action, "unknown", "preflight", None,
                    f"Target section could not be checked ({type(exc).__name__})",
                )
                continue
            if _target_has_existing_data(target.status_code, target.body):
                self._set_action(
                    ledger, court_slug, action, "blocked", "preflight", target.status_code,
                    "Target section already contains FaCT data; retained for human review",
                )
            elif target.status_code in {200, 204, 404}:
                self._set_action(ledger, court_slug, action, "ready", "preflight", target.status_code, None)
            else:
                self._set_action(
                    ledger, court_slug, action, "unknown", "preflight", target.status_code,
                    "Target section preflight returned an unexpected response",
                )
        self._update_court_status(court_state, record.get("actions", []))

    def _write_action(
        self,
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
                ledger, court_slug, action, "unknown", "execute", None,
                "Court UUID was unavailable after a successful preflight",
            )
            self._update_court_status(self._court_state(ledger, court_slug), record.get("actions", []))
            return
        body = self._execution_body(action, court_id)
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
            self._update_court_status(self._court_state(ledger, court_slug), record.get("actions", []))
            return
        try:
            response = client.write(action["method"], action["path"], body)
        except httpx.TimeoutException:
            self._set_action(ledger, court_slug, action, "unknown", "execute", None, "Write timed out; outcome is unknown")
            return
        except httpx.HTTPError as exc:
            self._set_action(ledger, court_slug, action, "failed", "execute", None, f"Write failed ({type(exc).__name__})")
            return
        if 200 <= response.status_code < 300:
            self._set_action(ledger, court_slug, action, "succeeded", "execute", response.status_code, None)
        else:
            self._set_action(
                ledger, court_slug, action, "failed", "execute", response.status_code,
                _write_rejection_reason(response.status_code, response.body),
            )
        self._update_court_status(self._court_state(ledger, court_slug), record.get("actions", []))

    @staticmethod
    def _execution_body(action: dict[str, Any], court_id: str) -> dict[str, Any]:
        """Use the freshly resolved UUID without modifying the immutable report."""

        body = dict(action.get("body") or {})
        if action.get("resource") != "professional_information":
            body["courtId"] = court_id
        return body

    def _court_state(self, ledger: ExecutionLedger, slug: str, court_id: str | None = None) -> CourtExecutionState:
        if slug not in ledger.courts:
            ledger.courts[slug] = CourtExecutionState(court_slug=slug, court_id=court_id)
        state = ledger.courts[slug]
        if court_id:
            state.court_id = court_id
        return state

    def _action_state(self, ledger: ExecutionLedger, slug: str, action_id: str) -> ActionExecutionState:
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

    def _update_court_status(self, court: CourtExecutionState, actions: Iterable[dict[str, Any]]) -> None:
        action_ids = [str(action["action_id"]) for action in actions]
        statuses = [court.actions[action_id].status for action_id in action_ids if action_id in court.actions]
        if action_ids and len(statuses) == len(action_ids) and all(status == "succeeded" for status in statuses):
            court.status = "completed"
        elif any(status in {"blocked", "failed", "unknown"} for status in statuses):
            court.status = "attention_required"
        elif statuses:
            court.status = "in_progress"
        else:
            court.status = "not_started"


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

    field_errors = []
    for field, message in body.items():
        if field in {"timestamp", "message"} or not isinstance(message, str):
            continue
        field_errors.append(f"{field}: {message}")
        if len(field_errors) == 3:
            break
    return f"{prefix}: {'; '.join(field_errors)}" if field_errors else prefix
