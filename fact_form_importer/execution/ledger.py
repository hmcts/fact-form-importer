"""Persistent sidecar ledger; generated archives are intentionally immutable."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from threading import Lock

from fact_form_importer.execution.atomic_state import atomic_write_json, file_lock
from fact_form_importer.execution.models import CourtExecutionState, ExecutionLedger, utc_now


class ExecutionLedgerStore:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self.directory = output_root / "execution-state"
        self._lock = Lock()

    def path_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def summary_path_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.summary.json"

    def court_directory_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.courts"

    def court_path_for(self, run_id: str, court_slug: str) -> Path:
        digest = hashlib.sha256(court_slug.encode("utf-8")).hexdigest()
        return self.court_directory_for(run_id) / f"{digest}.json"

    @property
    def latest_summary_path(self) -> Path:
        return self.output_root / "execution_summary.json"

    def load(self, run_id: str) -> ExecutionLedger:
        path = self.path_for(run_id)
        with self._lock, file_lock(path):
            return self._load_unlocked(run_id)

    def _load_unlocked(self, run_id: str) -> ExecutionLedger:
        path = self.path_for(run_id)
        has_consolidated = path.exists()
        if has_consolidated:
            ledger = ExecutionLedger.model_validate_json(path.read_text(encoding="utf-8"))
        else:
            ledger = ExecutionLedger(run_id=run_id)
        consolidated_at = ledger.updated_at
        for court_path in sorted(self.court_directory_for(run_id).glob("*.json")):
            try:
                payload = json.loads(court_path.read_text(encoding="utf-8"))
                saved_at = str(payload["saved_at"])
                court = CourtExecutionState.model_validate(payload["court"])
            except (KeyError, OSError, TypeError, ValueError):
                continue
            if not has_consolidated or saved_at >= consolidated_at:
                ledger.courts[court.court_slug] = court
                if saved_at > ledger.updated_at:
                    ledger.updated_at = saved_at
        return ledger

    def save(self, ledger: ExecutionLedger) -> ExecutionLedger:
        self.directory.mkdir(parents=True, exist_ok=True)
        ledger.updated_at = utc_now()
        path = self.path_for(ledger.run_id)
        with self._lock, file_lock(path):
            self._write_json(path, ledger.model_dump(mode="json"))
        return ledger

    def save_court(
        self, run_id: str, court: CourtExecutionState
    ) -> ExecutionLedger:
        """Checkpoint one court without rewriting the complete run ledger."""

        path = self.court_path_for(run_id, court.court_slug)
        saved_at = utc_now()
        court_copy = court.model_copy(deep=True)
        with file_lock(path):
            self._write_json(
                path,
                {
                    "shard_version": "1.0",
                    "run_id": run_id,
                    "saved_at": saved_at,
                    "court": court_copy.model_dump(mode="json"),
                },
            )
        return ExecutionLedger(
            run_id=run_id,
            updated_at=saved_at,
            courts={court_copy.court_slug: court_copy},
        )

    def load_summary(self, run_id: str) -> dict[str, Any] | None:
        path = self.summary_path_for(run_id)
        if not path.exists():
            return None
        with self._lock, file_lock(path):
            return json.loads(path.read_text(encoding="utf-8"))

    def save_summary(self, run_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock, file_lock(self.summary_path_for(run_id)):
            self._write_json(self.summary_path_for(run_id), summary)
        with self._lock, file_lock(self.latest_summary_path):
            self._write_json(self.latest_summary_path, summary)
        return summary

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        atomic_write_json(path, payload)
