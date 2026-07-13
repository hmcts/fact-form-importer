"""Localhost-only Flask application for archived FaCT importer runs."""

from __future__ import annotations

import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from fact_form_importer.config import AppConfig
from fact_form_importer.execution.service import ApiExecutionService
from fact_form_importer.ingest.column_mapping import load_column_mapping
from fact_form_importer.output.archive import load_run_archive, list_run_archives
from fact_form_importer.processing import ProcessingResult, process_workbook

PAGE_SIZE = 50
ALLOWED_EXTENSIONS = {".csv", ".xlsx"}


@dataclass(frozen=True)
class JobState:
    job_id: str
    state: str
    source_name: str
    use_llm: bool
    verify_addresses: bool = False
    run_id: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "source_name": self.source_name,
            "use_llm": self.use_llm,
            "verify_addresses": self.verify_addresses,
            "run_id": self.run_id,
            "error": self.error,
        }


Processor = Callable[..., ProcessingResult]


class LocalJobRunner:
    """One local import job at a time, with safe persisted status only."""

    def __init__(self, output_root: Path, processor: Processor) -> None:
        self.output_root = output_root
        self.processor = processor
        self.jobs_path = output_root / ".jobs"
        self.uploads_path = output_root / ".uploads"
        self.jobs_path.mkdir(parents=True, exist_ok=True)
        self.uploads_path.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fact-import")
        self.active_job_id: str | None = None
        self._restore_interrupted_jobs()

    def start(
        self,
        file: FileStorage,
        use_llm: bool,
        llm_enabled: bool,
        verify_addresses: bool = False,
        address_verification_available: bool = True,
    ) -> JobState:
        if self.active_job_id:
            raise ValueError("An import job is already running")
        if use_llm and not llm_enabled:
            raise ValueError("LLM processing is disabled by LLM_ENABLED")
        if verify_addresses and not address_verification_available:
            raise ValueError(
                "Address verification requires FACT_DATA_API_BASE_URL and FACT_DATA_API_BEARER_TOKEN"
            )
        source_name = secure_filename(file.filename or "")
        if not source_name or Path(source_name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError("Upload a CSV or XLSX file")

        job_id = uuid.uuid4().hex
        upload_directory = self.uploads_path / job_id
        upload_directory.mkdir(parents=True)
        input_path = upload_directory / source_name
        file.save(input_path)
        job = JobState(
            job_id=job_id,
            state="queued",
            source_name=source_name,
            use_llm=use_llm,
            verify_addresses=verify_addresses,
        )
        self._write_job(job)
        self.active_job_id = job_id
        self.executor.submit(self._run, job, input_path)
        return job

    def get(self, job_id: str) -> JobState | None:
        path = self.jobs_path / f"{job_id}.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return JobState(**payload)

    def _run(self, queued_job: JobState, input_path: Path) -> None:
        self._write_job(JobState(**{**queued_job.as_dict(), "state": "running"}))
        try:
            result = self.processor(
                input_path=input_path,
                output_root=self.output_root,
                use_llm=queued_job.use_llm,
                verify_addresses=queued_job.verify_addresses,
                source_name=queued_job.source_name,
            )
            self._write_job(
                JobState(
                    job_id=queued_job.job_id,
                    state="completed",
                    source_name=queued_job.source_name,
                    use_llm=queued_job.use_llm,
                    verify_addresses=queued_job.verify_addresses,
                    run_id=result.run_id,
                )
            )
        except Exception as exc:
            self._write_job(
                JobState(
                    job_id=queued_job.job_id,
                    state="failed",
                    source_name=queued_job.source_name,
                    use_llm=queued_job.use_llm,
                    verify_addresses=queued_job.verify_addresses,
                    error=_safe_job_error(exc),
                )
            )
        finally:
            shutil.rmtree(input_path.parent, ignore_errors=True)
            self.active_job_id = None

    def _write_job(self, job: JobState) -> None:
        path = self.jobs_path / f"{job.job_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(job.as_dict(), indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    def _restore_interrupted_jobs(self) -> None:
        for path in self.jobs_path.glob("*.json"):
            job = self.get(path.stem)
            if job and job.state in {"queued", "running"}:
                self._write_job(
                    JobState(
                        job_id=job.job_id,
                        state="failed",
                        source_name=job.source_name,
                        use_llm=job.use_llm,
                        verify_addresses=job.verify_addresses,
                        error="Server restarted before the import job completed",
                    )
                )


def create_app(
    output_root: Path,
    *,
    processor: Processor = process_workbook,
    config: AppConfig | None = None,
    execution_service: ApiExecutionService | None = None,
) -> Flask:
    """Create a local review application over immutable `out/final` archives."""

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
    app.config["OUTPUT_ROOT"] = output_root
    app.config["APP_CONFIG"] = config or AppConfig()
    app.config["EXECUTION_SERVICE"] = execution_service or ApiExecutionService(
        output_root, app.config["APP_CONFIG"]
    )
    app.config["JOB_RUNNER"] = LocalJobRunner(output_root, processor)

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            archives=list_run_archives(output_root),
            llm_enabled=app.config["APP_CONFIG"].llm_enabled,
            address_verification_available=_address_verification_available(app.config["APP_CONFIG"]),
        )

    @app.post("/runs")
    def start_run():
        upload = request.files.get("source_file")
        if upload is None:
            abort(400, "Choose a CSV or XLSX file")
        try:
            job = app.config["JOB_RUNNER"].start(
                upload,
                use_llm=request.form.get("use_llm") == "on",
                llm_enabled=app.config["APP_CONFIG"].llm_enabled,
                verify_addresses=request.form.get("verify_addresses") == "on",
                address_verification_available=_address_verification_available(
                    app.config["APP_CONFIG"]
                ),
            )
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(url_for("job_detail", job_id=job.job_id))

    @app.get("/jobs/<job_id>")
    def job_detail(job_id: str):
        job = app.config["JOB_RUNNER"].get(job_id)
        if job is None:
            abort(404)
        return render_template("job.html", job=job)

    @app.get("/jobs/<job_id>/status")
    def job_status(job_id: str):
        job = app.config["JOB_RUNNER"].get(job_id)
        if job is None:
            abort(404)
        return jsonify(job.as_dict())

    @app.get("/runs/<run_id>")
    def run_detail(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        primary_artifacts, report_artifacts, other_artifacts = _artifact_groups(archive)
        return render_template(
            "run_detail.html",
            archive=archive,
            primary_artifacts=primary_artifacts,
            report_artifacts=report_artifacts,
            other_artifacts=other_artifacts,
        )

    @app.get("/runs/<run_id>/records")
    def records(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        submissions = _load_json(archive["path"] / "submissions_cleaned.json", [])
        status = request.args.get("status")
        query = (request.args.get("q") or "").strip().casefold()
        filtered = [
            submission
            for submission in submissions
            if (not status or submission.get("status") == status)
            and _matches_record_query(submission, query)
        ]
        ledger = app.config["EXECUTION_SERVICE"].get_ledger(run_id)
        for submission in filtered:
            court = ledger.courts.get(submission.get("court_slug"))
            submission["execution_status"] = court.status if court else "not_started"
        page, pages, records_page = _paginate(filtered, request.args.get("page"))
        return render_template(
            "records.html",
            archive=archive,
            records=records_page,
            status=status,
            query=query,
            page=page,
            pages=pages,
        )

    @app.get("/runs/<run_id>/records/<int:source_row_number>")
    def record_detail(run_id: str, source_row_number: int):
        archive = _archive_or_404(output_root, run_id)
        submissions = _load_json(archive["path"] / "submissions_cleaned.json", [])
        submission = next(
            (
                item
                for item in submissions
                if item.get("source", {}).get("source_row_number") == source_row_number
            ),
            None,
        )
        if submission is None:
            abort(404)
        manifest = _load_readiness_report(archive["path"])
        actions = next(
            (
                item.get("actions", [])
                for item in manifest.get("records", [])
                if source_row_number in item.get("source_row_numbers", [])
            ),
            [],
        )
        ledger = app.config["EXECUTION_SERVICE"].get_ledger(run_id)
        court_execution = ledger.courts.get(submission.get("court_slug"))
        actions = [
            {
                "body": action.get("body", {}),
                **action,
                "execution": (
                    court_execution.actions.get(action.get("action_id")).model_dump(mode="json")
                    if court_execution and action.get("action_id") in court_execution.actions
                    else {"status": "planned"}
                ),
                "evidence": _action_evidence(submission, action),
            }
            for action in actions
        ]
        return render_template(
            "record_detail.html",
            archive=archive,
            submission=submission,
            actions=actions,
            court_execution=court_execution,
            writes_enabled=app.config["APP_CONFIG"].fact_data_api_writes_enabled,
        )

    @app.get("/runs/<run_id>/issues")
    def issues(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        issues = _load_json(archive["path"] / "issue_report.json", [])
        code = request.args.get("code")
        if code:
            issues = [issue for issue in issues if issue.get("code") == code]
        page, pages, issues_page = _paginate(issues, request.args.get("page"))
        return render_template(
            "issues.html", archive=archive, issues=issues_page, code=code, page=page, pages=pages
        )

    @app.get("/runs/<run_id>/api-actions")
    def api_actions(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        manifest = _load_readiness_report(archive["path"])
        readiness = request.args.get("readiness")
        actions = [
            {
                "court_slug": record.get("court_slug"),
                "execution_status": _action_execution_status(
                    app.config["EXECUTION_SERVICE"].get_ledger(run_id),
                    record.get("court_slug"),
                    action.get("action_id"),
                ),
                **action,
            }
            for record in manifest.get("records", [])
            for action in record.get("actions", [])
            if not readiness or action.get("readiness") == readiness
        ]
        page, pages, actions_page = _paginate(actions, request.args.get("page"))
        return render_template(
            "api_actions.html",
            archive=archive,
            manifest=manifest,
            actions=actions_page,
            readiness=readiness,
            page=page,
            pages=pages,
            writes_enabled=app.config["APP_CONFIG"].fact_data_api_writes_enabled,
        )

    @app.post("/runs/<run_id>/courts/<court_slug>/api-check")
    def api_check_court(run_id: str, court_slug: str):
        _archive_or_404(output_root, run_id)
        try:
            app.config["EXECUTION_SERVICE"].check_court(run_id, court_slug)
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(url_for("record_detail", run_id=run_id, source_row_number=request.form["source_row_number"]))

    @app.post("/runs/<run_id>/courts/<court_slug>/actions/<action_id>/execute")
    def api_execute_action(run_id: str, court_slug: str, action_id: str):
        _archive_or_404(output_root, run_id)
        if not app.config["APP_CONFIG"].fact_data_api_writes_enabled:
            abort(403, "FaCT API writes are disabled by FACT_DATA_API_WRITES_ENABLED")
        try:
            app.config["EXECUTION_SERVICE"].execute_action(run_id, court_slug, action_id)
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(url_for("record_detail", run_id=run_id, source_row_number=request.form["source_row_number"]))

    @app.post("/runs/<run_id>/courts/<court_slug>/execute-safe")
    def api_execute_court(run_id: str, court_slug: str):
        _archive_or_404(output_root, run_id)
        if not app.config["APP_CONFIG"].fact_data_api_writes_enabled:
            abort(403, "FaCT API writes are disabled by FACT_DATA_API_WRITES_ENABLED")
        try:
            app.config["EXECUTION_SERVICE"].execute_safe_court_actions(run_id, court_slug)
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(url_for("record_detail", run_id=run_id, source_row_number=request.form["source_row_number"]))

    @app.get("/runs/<run_id>/download/<artifact_name>")
    def download(run_id: str, artifact_name: str):
        archive = _archive_or_404(output_root, run_id)
        allowed_names = {item["name"] for item in archive["manifest"].get("artifacts", [])}
        if artifact_name not in allowed_names:
            abort(404)
        artifact_path = archive["path"] / artifact_name
        if not artifact_path.is_file():
            abort(404)
        return send_from_directory(archive["path"].resolve(), artifact_name, as_attachment=True)

    return app


def run_server(output_root: Path, host: str = "127.0.0.1", port: int = 5000) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The review UI may only bind to localhost")
    create_app(output_root).run(host=host, port=port, debug=False)


def _archive_or_404(output_root: Path, run_id: str) -> dict[str, Any]:
    archive = load_run_archive(output_root, run_id)
    if archive is None:
        abort(404)
    return archive


def _load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def _load_readiness_report(archive_path: Path) -> dict[str, Any]:
    """Load the current report name, retaining historic archive readability."""

    current = archive_path / "api_readiness_report.json"
    if current.exists():
        return _load_json(current, {})
    return _load_json(archive_path / "fact_api_import_manifest.json", {})


def _artifact_groups(archive: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    labels = {
        "fact_import_payload.json": "Import payload",
        "fact_payload.json": "Legacy import payload",
        "api_readiness_report.json": "API readiness report",
        "fact_api_import_manifest.json": "Legacy API readiness report",
        "nsu_cleaned_review.xlsx": "NSU cleaned review workbook",
        "duplicate_forms_review.xlsx": "Duplicate form decision workbook",
        "address_verification_report.json": "Address verification report",
        "import_summary.json": "Import summary",
        "issue_report.json": "Issue report",
        "records_needing_human_review.json": "Records needing human review",
        "failed_records.json": "Failed records",
    }
    primary_names = {"fact_import_payload.json", "fact_payload.json"}
    report_names = {
        "api_readiness_report.json",
        "fact_api_import_manifest.json",
        "nsu_cleaned_review.xlsx",
        "duplicate_forms_review.xlsx",
        "address_verification_report.json",
        "import_summary.json",
        "issue_report.json",
        "records_needing_human_review.json",
        "failed_records.json",
    }
    primary: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for artifact in archive["manifest"].get("artifacts", []):
        item = {**artifact, "label": labels.get(artifact["name"], artifact["name"])}
        if artifact["name"] in primary_names:
            primary.append(item)
        elif artifact["name"] in report_names:
            reports.append(item)
        else:
            other.append(item)
    return primary, reports, other


def _matches_record_query(submission: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    values = [
        submission.get("court_slug"),
        submission.get("court_slug_raw"),
        submission.get("source", {}).get("source_row_number"),
    ]
    return any(query in str(value).casefold() for value in values if value is not None)


def _safe_job_error(exc: Exception) -> str:
    """Expose actionable local setup errors without persisting request details or secrets."""

    message = str(exc)
    if isinstance(exc, ValueError) and "401" in message and "FaCT API" in message:
        return (
            "FaCT API authentication failed. Refresh FACT_DATA_API_BEARER_TOKEN "
            "and restart the review UI."
        )
    return f"Processing failed ({type(exc).__name__})"


def _address_verification_available(config: AppConfig) -> bool:
    return bool(config.fact_data_api_base_url and config.fact_data_api_bearer_token)


def _paginate(items: list[Any], page_value: str | None) -> tuple[int, int, list[Any]]:
    try:
        page = max(int(page_value or "1"), 1)
    except ValueError:
        page = 1
    pages = max((len(items) + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    start = (page - 1) * PAGE_SIZE
    return page, pages, items[start : start + PAGE_SIZE]


def _action_execution_status(ledger, court_slug: str | None, action_id: str | None) -> str:
    court = ledger.courts.get(court_slug) if court_slug else None
    action = court.actions.get(action_id) if court and action_id else None
    return action.status if action else "planned"


def _action_evidence(submission: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """Show only the cleaned fields that generated an action plus raw group evidence."""

    source_fields = action.get("source_fields") or []
    return {
        "cleaned": {field: _value_at_path(submission, field) for field in source_fields},
        "raw": _raw_evidence_for_fields(submission.get("raw", {}), source_fields),
        "address_verification": action.get("address_verification"),
        "request_body_normalisations": action.get("request_body_normalisations") or {},
    }


def _value_at_path(value: Any, path: str) -> Any:
    current = value
    for part in path.replace("]", "").replace("[", ".").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = next(
                (item for item in current if isinstance(item, dict) and item.get("index") == int(part)),
                None,
            )
        else:
            return None
    return current


def _raw_evidence_for_fields(raw: dict[str, Any], source_fields: list[str]) -> dict[str, Any]:
    """Map action sections to their Microsoft Forms column values where known."""

    mapping_path = Path(__file__).resolve().parents[2] / "config" / "column_mapping.json"
    if not mapping_path.exists() or not isinstance(raw, dict):
        return raw
    mapping = load_column_mapping(mapping_path)
    columns: set[str] = set()
    for field in source_fields:
        root = field.split("[", 1)[0].split(".", 1)[0]
        child = field.split(".", 1)[1] if "." in field else ""
        if root == "facilities" and child:
            columns.update(ref.column for ref in mapping.scalars if ref.field == child)
        elif root in {"translation_phone", "translation_email"}:
            columns.update(ref.column for ref in mapping.scalars if ref.field == root)
        elif root == "counter_service":
            columns.update(ref.column for ref in mapping.counter_service)
        elif root == "interview_rooms":
            columns.update(ref.column for ref in mapping.interview_rooms)
        elif root in {"addresses", "contacts", "opening_hours"}:
            index_text = field.split("[", 1)[1].split("]", 1)[0] if "[" in field else ""
            if not index_text.isdigit():
                continue
            groups = {
                "addresses": mapping.address_groups,
                "contacts": mapping.contact_detail_groups,
                "opening_hours": mapping.opening_hours_groups,
            }[root]
            group = next((item for item in groups if item.index == int(index_text)), None)
            if group:
                columns.update(ref.column for ref in group.columns)
    return {column: raw.get(column) for column in sorted(columns) if raw.get(column) is not None}
