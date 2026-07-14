import json
from threading import Event

import pytest

from fact_form_importer.execution.jobs import ExecutionJob, ExecutionJobRunner


class _HoldingService:
    def __init__(self):
        self.started = Event()
        self.release = Event()
        self.calls = []

    def execute_action(self, run_id, court_slug, action_id):
        self.calls.append(("action", run_id, court_slug, action_id))
        self.started.set()
        self.release.wait(timeout=2)

    def execute_safe_court_actions(self, run_id, court_slug):
        self.calls.append(("court", run_id, court_slug))

    def execute_all_safe_actions(self, run_id):
        self.calls.append(("run", run_id))

    def refresh_all_target_comparisons(self, run_id):
        self.calls.append(("comparison", run_id))


def test_execution_jobs_are_globally_exclusive_and_persist_terminal_state(tmp_path):
    service = _HoldingService()
    runner = ExecutionJobRunner(tmp_path, service)

    job = runner.start("run-1", "action", court_slug="court", action_id="action-1")
    assert service.started.wait(timeout=1)
    with pytest.raises(ValueError, match="already running"):
        runner.start("run-2", "run")

    service.release.set()
    runner.executor.shutdown(wait=True)

    completed = runner.get(job.job_id)
    assert completed is not None
    assert completed.state == "completed"
    assert completed.started_at is not None
    assert completed.completed_at is not None
    assert service.calls == [("action", "run-1", "court", "action-1")]


def test_runner_marks_unknown_inflight_jobs_interrupted_after_restart(tmp_path):
    directory = tmp_path / ".execution-jobs"
    directory.mkdir()
    job = ExecutionJob(job_id="inflight", run_id="run", scope="run", state="running")
    (directory / "inflight.json").write_text(
        json.dumps(job.model_dump(mode="json")), encoding="utf-8"
    )

    runner = ExecutionJobRunner(tmp_path, _HoldingService())
    restored = runner.get("inflight")
    runner.executor.shutdown(wait=True)

    assert restored is not None
    assert restored.state == "interrupted"
    assert "restarted" in str(restored.error)
