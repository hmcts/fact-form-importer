"""Persistent sidecar ledger; generated archives are intentionally immutable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from threading import Lock

from fact_form_importer.execution.models import ExecutionLedger, utc_now


class ExecutionLedgerStore:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self.directory = output_root / "execution-state"
        self._lock = Lock()

    def path_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def summary_path_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.summary.json"

    @property
    def latest_summary_path(self) -> Path:
        return self.output_root / "execution_summary.json"

    def load(self, run_id: str) -> ExecutionLedger:
        path = self.path_for(run_id)
        if not path.exists():
            return ExecutionLedger(run_id=run_id)
        return ExecutionLedger.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, ledger: ExecutionLedger) -> ExecutionLedger:
        self.directory.mkdir(parents=True, exist_ok=True)
        ledger.updated_at = utc_now()
        path = self.path_for(ledger.run_id)
        with self._lock:
            self._write_json(path, ledger.model_dump(mode="json"))
        return ledger

    def load_summary(self, run_id: str) -> dict[str, Any] | None:
        path = self.summary_path_for(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_summary(self, run_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._write_json(self.summary_path_for(run_id), summary)
            self._write_json(self.latest_summary_path, summary)
        return summary

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temp_path.replace(path)
