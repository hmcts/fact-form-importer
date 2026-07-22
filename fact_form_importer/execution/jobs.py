"""Persistent single-worker jobs for local FaCT execution controls."""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from fact_form_importer.execution.atomic_state import atomic_write_json, file_lock
from fact_form_importer.execution.models import utc_now
from fact_form_importer.execution.service import ApiExecutionService


class ExecutionJob(BaseModel):
    job_version: str = "1.1"
    job_id: str
    run_id: str
    scope: Literal["action", "court", "run", "comparison"]
    court_slug: Optional[str] = None
    action_id: Optional[str] = None
    state: Literal["queued", "running", "completed", "failed", "interrupted"] = "queued"
    created_at: str = Field(default_factory=utc_now)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    owner_pid: Optional[int] = None
    heartbeat_at: Optional[str] = None
    progress: dict[str, int] = Field(default_factory=dict)
    timing: dict[str, float] = Field(default_factory=dict)
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
        self._global_lock_context: Any = None
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
            if self.active():
                raise ValueError("Another FaCT execution job is already running")
            if scope in {"action", "court"} and not court_slug:
                raise ValueError("Court slug is required for this execution job")
            if scope == "action" and not action_id:
                raise ValueError("Action ID is required for an action execution job")
            lock_context = file_lock(self.directory / "global", blocking=False)
            try:
                lock_context.__enter__()
            except BlockingIOError as exc:
                raise ValueError("Another FaCT execution job is already running") from exc
            job = ExecutionJob(
                job_id=uuid.uuid4().hex,
                run_id=run_id,
                scope=scope,
                court_slug=court_slug,
                action_id=action_id,
                owner_pid=os.getpid(),
                heartbeat_at=utc_now(),
            )
            try:
                self._save(job)
                self.active_job_id = job.job_id
                self._global_lock_context = lock_context
                self.executor.submit(self._run, job)
            except Exception:
                lock_context.__exit__(None, None, None)
                raise
            return job

    def get(self, job_id: str) -> ExecutionJob | None:
        path = self.directory / f"{job_id}.json"
        if not path.exists():
            return None
        return ExecutionJob.model_validate_json(path.read_text(encoding="utf-8"))

    def active(self) -> ExecutionJob | None:
        if self.active_job_id:
            local = self.get(self.active_job_id)
            if local and local.state in {"queued", "running"}:
                return local
        active_jobs = [
            job
            for path in self.directory.glob("*.json")
            if (job := self._load_path(path)) is not None
            and job.state in {"queued", "running"}
        ]
        return max(active_jobs, key=lambda job: job.created_at) if active_jobs else None

    def latest_for_run(self, run_id: str) -> ExecutionJob | None:
        jobs = []
        for path in self.directory.glob("*.json"):
            try:
                job = self._load_path(path)
            except OSError:
                continue
            if job and job.run_id == run_id:
                jobs.append(job)
        return max(jobs, key=lambda job: job.created_at) if jobs else None

    def _run(self, job: ExecutionJob) -> None:
        running = job.model_copy(
            update={"state": "running", "started_at": utc_now(), "heartbeat_at": utc_now()}
        )
        self._save(running)
        started = time.monotonic()
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
                self.service.execute_all_safe_actions(
                    running.run_id,
                    progress_callback=lambda completed, total: self._progress(
                        running.job_id, completed, total
                    ),
                )
            else:
                self.service.refresh_all_target_comparisons(running.run_id)
        except Exception as exc:
            current = self.get(running.job_id) or running
            finished = current.model_copy(
                update={
                    "state": "failed",
                    "completed_at": utc_now(),
                    "error": _safe_error(exc),
                    "heartbeat_at": utc_now(),
                    "timing": {"wall_seconds": round(time.monotonic() - started, 3)},
                }
            )
        else:
            current = self.get(running.job_id) or running
            finished = current.model_copy(
                update={
                    "state": "completed",
                    "completed_at": utc_now(),
                    "heartbeat_at": utc_now(),
                    "timing": {"wall_seconds": round(time.monotonic() - started, 3)},
                }
            )
        try:
            self._save(finished)
        finally:
            with self._lock:
                if self.active_job_id == job.job_id:
                    self.active_job_id = None
                lock_context = self._global_lock_context
                self._global_lock_context = None
                if lock_context is not None:
                    lock_context.__exit__(None, None, None)

    def _save(self, job: ExecutionJob) -> None:
        path = self.directory / f"{job.job_id}.json"
        atomic_write_json(path, job.model_dump(mode="json"))

    def _progress(self, job_id: str, completed: int, total: int) -> None:
        job = self.get(job_id)
        if job is None or job.state not in {"queued", "running"}:
            return
        self._save(
            job.model_copy(
                update={
                    "heartbeat_at": utc_now(),
                    "progress": {"completed_courts": completed, "total_courts": total},
                }
            )
        )

    @staticmethod
    def _load_path(path: Path) -> ExecutionJob | None:
        try:
            return ExecutionJob.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _restore_interrupted_jobs(self) -> None:
        context = file_lock(self.directory / "global", blocking=False)
        try:
            context.__enter__()
        except BlockingIOError:
            return
        try:
            for path in self.directory.glob("*.json"):
                job = self._load_path(path)
                if job and job.state in {"queued", "running"}:
                    self._save(
                        job.model_copy(
                            update={
                                "state": "interrupted",
                                "completed_at": utc_now(),
                                "heartbeat_at": utc_now(),
                                "error": "Server restarted after the owning importer process stopped before the job completed",
                            }
                        )
                    )
        finally:
            context.__exit__(None, None, None)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)[:500]
    return f"Execution job failed ({type(exc).__name__})"
