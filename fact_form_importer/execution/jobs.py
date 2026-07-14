"""Persistent single-worker jobs for local FaCT execution controls."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Literal, Optional

from pydantic import BaseModel, Field

from fact_form_importer.execution.models import utc_now
from fact_form_importer.execution.service import ApiExecutionService


class ExecutionJob(BaseModel):
    job_version: str = "1.0"
    job_id: str
    run_id: str
    scope: Literal["action", "court", "run", "comparison"]
    court_slug: Optional[str] = None
    action_id: Optional[str] = None
    state: Literal["queued", "running", "completed", "failed", "interrupted"] = "queued"
    created_at: str = Field(default_factory=utc_now)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None


class ExecutionJobRunner:
    """Allow only one write job globally so runs cannot race each other."""

    def __init__(self, output_root: Path, service: ApiExecutionService) -> None:
        self.directory = output_root / ".execution-jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fact-execution")
        self._lock = Lock()
        self.active_job_id: str | None = None
        self._restore_interrupted_jobs()

    def start(
        self,
        run_id: str,
        scope: Literal["action", "court", "run", "comparison"],
        *,
        court_slug: str | None = None,
        action_id: str | None = None,
    ) -> ExecutionJob:
        with self._lock:
            if self.active_job_id:
                raise ValueError("Another FaCT execution job is already running")
            if scope in {"action", "court"} and not court_slug:
                raise ValueError("Court slug is required for this execution job")
            if scope == "action" and not action_id:
                raise ValueError("Action ID is required for an action execution job")
            job = ExecutionJob(
                job_id=uuid.uuid4().hex,
                run_id=run_id,
                scope=scope,
                court_slug=court_slug,
                action_id=action_id,
            )
            self._save(job)
            self.active_job_id = job.job_id
            self.executor.submit(self._run, job)
            return job

    def get(self, job_id: str) -> ExecutionJob | None:
        path = self.directory / f"{job_id}.json"
        if not path.exists():
            return None
        return ExecutionJob.model_validate_json(path.read_text(encoding="utf-8"))

    def active(self) -> ExecutionJob | None:
        return self.get(self.active_job_id) if self.active_job_id else None

    def latest_for_run(self, run_id: str) -> ExecutionJob | None:
        jobs = []
        for path in self.directory.glob("*.json"):
            try:
                job = ExecutionJob.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.run_id == run_id:
                jobs.append(job)
        return max(jobs, key=lambda job: job.created_at) if jobs else None

    def _run(self, job: ExecutionJob) -> None:
        running = job.model_copy(update={"state": "running", "started_at": utc_now()})
        self._save(running)
        try:
            if running.scope == "action":
                self.service.execute_action(
                    running.run_id, str(running.court_slug), str(running.action_id)
                )
            elif running.scope == "court":
                self.service.execute_safe_court_actions(
                    running.run_id, str(running.court_slug)
                )
            elif running.scope == "run":
                self.service.execute_all_safe_actions(running.run_id)
            else:
                self.service.refresh_all_target_comparisons(running.run_id)
        except Exception as exc:
            finished = running.model_copy(
                update={
                    "state": "failed",
                    "completed_at": utc_now(),
                    "error": _safe_error(exc),
                }
            )
        else:
            finished = running.model_copy(
                update={"state": "completed", "completed_at": utc_now()}
            )
        self._save(finished)
        with self._lock:
            if self.active_job_id == job.job_id:
                self.active_job_id = None

    def _save(self, job: ExecutionJob) -> None:
        path = self.directory / f"{job.job_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(job.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _restore_interrupted_jobs(self) -> None:
        for path in self.directory.glob("*.json"):
            try:
                job = ExecutionJob.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.state in {"queued", "running"}:
                self._save(
                    job.model_copy(
                        update={
                            "state": "interrupted",
                            "completed_at": utc_now(),
                            "error": "Server restarted before the execution job completed",
                        }
                    )
                )


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)[:500]
    return f"Execution job failed ({type(exc).__name__})"
