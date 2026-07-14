"""Localhost-only Flask application for archived FaCT importer runs."""

from __future__ import annotations

import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from zipfile import ZIP_STORED, ZipFile

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from fact_form_importer.config import AppConfig
from fact_form_importer.execution.service import ApiExecutionService
from fact_form_importer.execution.jobs import ExecutionJobRunner
from fact_form_importer.ingest.column_mapping import load_column_mapping
from fact_form_importer.output.archive import load_run_archive, list_run_archives
from fact_form_importer.processing import ProcessingResult, process_workbook
from fact_form_importer.validators.base import (
    HUMAN_REVIEW_ISSUE_CODES,
    LLM_HUMAN_REVIEW_ISSUE_CODES,
)

PAGE_SIZE = 50
ALLOWED_EXTENSIONS = {".csv", ".xlsx"}
OS_ACTION_BLOCKING_STATUSES = {
    "review_required",
    "invalid_postcode",
    "unsupported_postcode_region",
    "no_os_result",
    "missing_postcode",
}


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
        temporary.write_text(json.dumps(job.as_dict(), indent=2) + "\n", encoding="utf-8")
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
    app.config["EXECUTION_JOB_RUNNER"] = ExecutionJobRunner(
        output_root, app.config["EXECUTION_SERVICE"]
    )
    app.config["JOB_RUNNER"] = LocalJobRunner(output_root, processor)

    @app.get("/")
    def index():
        archives = list_run_archives(output_root)
        for archive in archives:
            archive["factor_summary"] = _run_factor_summary(archive)
        return render_template(
            "index.html",
            archives=archives,
            llm_enabled=app.config["APP_CONFIG"].llm_enabled,
            address_verification_available=_address_verification_available(
                app.config["APP_CONFIG"]
            ),
            has_llm_review_factors=any(
                archive["factor_summary"]["llm_review_submission_count"] > 0 for archive in archives
            ),
            has_os_address_factors=any(
                archive["factor_summary"]["os_action_blocking_submission_count"] > 0
                for archive in archives
            ),
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
            factor_summary=_run_factor_summary(archive),
            execution_summary=app.config["EXECUTION_SERVICE"].get_execution_summary(run_id),
            writes_enabled=app.config["APP_CONFIG"].fact_data_api_writes_enabled,
            execution_job=app.config["EXECUTION_JOB_RUNNER"].latest_for_run(run_id),
            active_execution_job=app.config["EXECUTION_JOB_RUNNER"].active(),
        )

    @app.get("/runs/<run_id>/records")
    def records(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        submissions = _load_json(archive["path"] / "submissions_cleaned.json", [])
        status = request.args.get("status")
        category = request.args.get("category")
        query = (request.args.get("q") or "").strip().casefold()
        filtered = [
            submission
            for submission in submissions
            if (not status or submission.get("status") == status)
            and (not category or _submission_has_review_category(submission, category))
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
            category=category,
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
        manifest = app.config["EXECUTION_SERVICE"].get_readiness_report(run_id)
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
            if action.get("source_row_number") in {None, source_row_number}
        ]
        return render_template(
            "record_detail.html",
            archive=archive,
            submission=submission,
            actions=actions,
            court_execution=court_execution,
            writes_enabled=app.config["APP_CONFIG"].fact_data_api_writes_enabled,
            active_execution_job=app.config["EXECUTION_JOB_RUNNER"].active(),
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

    @app.get("/runs/<run_id>/llm-review-factors")
    def llm_review_factors(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        factors = _llm_review_factors(archive)
        page, pages, factors_page = _paginate(factors, request.args.get("page"))
        return render_template(
            "llm_review_factors.html",
            archive=archive,
            factors=factors_page,
            factor_summary=_run_factor_summary(archive),
            page=page,
            pages=pages,
        )

    @app.get("/runs/<run_id>/review")
    def review_overview(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        return render_template(
            "review_overview.html",
            archive=archive,
            overview=_review_overview(
                archive, app.config["EXECUTION_SERVICE"]
            ),
        )

    @app.get("/runs/<run_id>/llm-actions")
    def llm_actions_review(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        payload = app.config["EXECUTION_SERVICE"].get_llm_actions_review(run_id)
        status = request.args.get("status")
        queue = request.args.get("queue")
        confidence = request.args.get("confidence")
        if confidence not in {None, "", "high", "medium", "low", "unavailable"}:
            confidence = None
        query = (request.args.get("q") or "").strip().casefold()
        items = sorted(
            [
            item
            for item in payload.get("items", [])
            if (not status or item.get("approval_status") == status)
            and (not queue or queue == "llm")
            and (
                not confidence
                or _review_confidence(item) == confidence
            )
            and (
                not query
                or query in str(item.get("court_slug") or "").casefold()
                or query in str(item.get("source_row_number") or "").casefold()
                or query in str(item.get("field") or "").casefold()
                or query in str(item.get("review_id") or "").casefold()
            )
            ],
            key=_llm_review_sort_key,
        )
        active_job = app.config["EXECUTION_JOB_RUNNER"].active()
        for item in items:
            item["address_editable"] = bool(
                item.get("kind") == "address"
                and item.get("approvable", item.get("actionable"))
                and not active_job
                and not {
                    action.get("status")
                    for action in item.get("dependent_actions", [])
                }
                & {"running", "succeeded", "unknown"}
            )
        field_items = [item for item in items if item.get("kind") == "field"]
        address_items = [item for item in items if item.get("kind") == "address"]
        field_page, field_pages, field_page_items = _paginate(
            field_items, request.args.get("field_page")
        )
        address_page, address_pages, address_page_items = _paginate(
            address_items, request.args.get("address_page")
        )
        return render_template(
            "llm_actions_review.html",
            archive=archive,
            review=payload,
            fields=field_page_items,
            addresses=address_page_items,
            status=status,
            queue=queue,
            confidence=confidence or "",
            query=request.args.get("q") or "",
            field_page=field_page,
            field_pages=field_pages,
            address_page=address_page,
            address_pages=address_pages,
            approval_complete=request.args.get("complete") == "1",
            active_execution_job=active_job,
        )

    @app.post("/runs/<run_id>/llm-actions/<review_id>/approve")
    def approve_llm_action(run_id: str, review_id: str):
        _archive_or_404(output_root, run_id)
        address_patch = None
        if "addressLine1" in request.form:
            address_patch = {
                field: request.form.get(field)
                for field in (
                    "addressLine1",
                    "addressLine2",
                    "townCity",
                    "county",
                    "postcode",
                )
            }
        try:
            app.config["EXECUTION_SERVICE"].approve_llm_review(
                run_id,
                review_id,
                address_patch=address_patch,
                execution_job_active=bool(
                    app.config["EXECUTION_JOB_RUNNER"].active()
                ),
            )
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(
            _next_llm_review_url(
                app.config["EXECUTION_SERVICE"], run_id, review_id, request.form
            )
        )

    @app.get("/runs/<run_id>/api-changes")
    def api_changes_review(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        payload = app.config["EXECUTION_SERVICE"].get_api_changes_review(run_id)
        hold = request.args.get("hold")
        changes = payload["changes"]
        if hold:
            changes = [change for change in changes if _change_has_hold(change, hold)]
        page, pages, changes_page = _paginate(changes, request.args.get("page"))
        return render_template(
            "api_changes_review.html",
            archive=archive,
            changes=changes_page,
            page=page,
            pages=pages,
            hold=hold,
            comparison_job=app.config["EXECUTION_JOB_RUNNER"].latest_for_run(run_id),
            active_execution_job=app.config["EXECUTION_JOB_RUNNER"].active(),
        )

    @app.post("/runs/<run_id>/api-changes/refresh")
    def refresh_api_changes(run_id: str):
        _archive_or_404(output_root, run_id)
        try:
            job = app.config["EXECUTION_JOB_RUNNER"].start(run_id, "comparison")
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(url_for("api_changes_review", run_id=run_id, job_id=job.job_id))

    @app.post("/runs/<run_id>/api-changes/<action_id>/refresh")
    def refresh_api_change(run_id: str, action_id: str):
        _archive_or_404(output_root, run_id)
        court_slug = request.form.get("court_slug") or ""
        try:
            app.config["EXECUTION_SERVICE"].refresh_target_comparison(
                run_id, court_slug, action_id
            )
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(url_for("api_changes_review", run_id=run_id))

    @app.post("/runs/<run_id>/api-changes/<change_id>/approve")
    def approve_api_change(run_id: str, change_id: str):
        _archive_or_404(output_root, run_id)
        try:
            app.config["EXECUTION_SERVICE"].approve_target_change(run_id, change_id)
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(url_for("api_changes_review", run_id=run_id))

    @app.post("/runs/<run_id>/courts/<court_slug>/select-source")
    def select_duplicate_source(run_id: str, court_slug: str):
        _archive_or_404(output_root, run_id)
        try:
            source_row_number = int(request.form.get("source_row_number") or "")
            app.config["EXECUTION_SERVICE"].select_source_row(
                run_id, court_slug, source_row_number
            )
        except (TypeError, ValueError) as exc:
            abort(400, str(exc))
        return redirect(url_for("api_changes_review", run_id=run_id))

    @app.get("/runs/<run_id>/os-address-factors")
    def os_address_factors(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        factors = _os_address_factors(archive)
        page, pages, factors_page = _paginate(factors, request.args.get("page"))
        return render_template(
            "os_address_factors.html",
            archive=archive,
            factors=factors_page,
            factor_summary=_run_factor_summary(archive),
            page=page,
            pages=pages,
        )

    @app.get("/runs/<run_id>/api-actions")
    def api_actions(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        manifest = app.config["EXECUTION_SERVICE"].get_readiness_report(run_id)
        readiness = request.args.get("readiness")
        ledger = app.config["EXECUTION_SERVICE"].get_ledger(run_id)

        actions = [
            {
                "court_slug": record.get("court_slug"),
                "execution_status": _action_execution_status(
                    ledger, record.get("court_slug"), action.get("action_id")
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

    @app.get("/runs/<run_id>/execution-summary")
    def execution_summary(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        requested_job = request.args.get("job_id")
        job = (
            app.config["EXECUTION_JOB_RUNNER"].get(requested_job)
            if requested_job
            else app.config["EXECUTION_JOB_RUNNER"].latest_for_run(run_id)
        )
        return render_template(
            "execution_summary.html",
            archive=archive,
            execution_summary=app.config["EXECUTION_SERVICE"].get_execution_summary(run_id),
            execution_job=job,
            active_execution_job=app.config["EXECUTION_JOB_RUNNER"].active(),
            writes_enabled=app.config["APP_CONFIG"].fact_data_api_writes_enabled,
        )

    @app.get("/execution-jobs/<job_id>/status.json")
    def execution_job_status(job_id: str):
        job = app.config["EXECUTION_JOB_RUNNER"].get(job_id)
        if job is None:
            abort(404)
        summary = app.config["EXECUTION_SERVICE"].get_execution_summary(job.run_id)
        return jsonify({"job": job.model_dump(mode="json"), "execution_summary": summary})

    @app.get("/runs/<run_id>/execution-summary.json")
    def execution_summary_json(run_id: str):
        _archive_or_404(output_root, run_id)
        payload = app.config["EXECUTION_SERVICE"].get_execution_summary(run_id)
        return Response(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            mimetype="application/json",
            headers={
                "Content-Disposition": (f'attachment; filename="{run_id}-execution-summary.json"')
            },
        )

    @app.post("/runs/<run_id>/courts/<court_slug>/api-check")
    def api_check_court(run_id: str, court_slug: str):
        _archive_or_404(output_root, run_id)
        try:
            app.config["EXECUTION_SERVICE"].check_court(run_id, court_slug)
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(
            url_for(
                "record_detail", run_id=run_id, source_row_number=request.form["source_row_number"]
            )
        )

    @app.post("/runs/<run_id>/courts/<court_slug>/actions/<action_id>/execute")
    def api_execute_action(run_id: str, court_slug: str, action_id: str):
        _archive_or_404(output_root, run_id)
        if not app.config["APP_CONFIG"].fact_data_api_writes_enabled:
            abort(403, "FaCT API writes are disabled by FACT_DATA_API_WRITES_ENABLED")
        try:
            job = app.config["EXECUTION_JOB_RUNNER"].start(
                run_id, "action", court_slug=court_slug, action_id=action_id
            )
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(url_for("execution_summary", run_id=run_id, job_id=job.job_id))

    @app.post("/runs/<run_id>/courts/<court_slug>/execute-safe")
    def api_execute_court(run_id: str, court_slug: str):
        _archive_or_404(output_root, run_id)
        if not app.config["APP_CONFIG"].fact_data_api_writes_enabled:
            abort(403, "FaCT API writes are disabled by FACT_DATA_API_WRITES_ENABLED")
        try:
            job = app.config["EXECUTION_JOB_RUNNER"].start(
                run_id, "court", court_slug=court_slug
            )
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(url_for("execution_summary", run_id=run_id, job_id=job.job_id))

    @app.post("/runs/<run_id>/execute-safe")
    def api_execute_run(run_id: str):
        _archive_or_404(output_root, run_id)
        if not app.config["APP_CONFIG"].fact_data_api_writes_enabled:
            abort(403, "FaCT API writes are disabled by FACT_DATA_API_WRITES_ENABLED")
        try:
            job = app.config["EXECUTION_JOB_RUNNER"].start(run_id, "run")
        except ValueError as exc:
            abort(400, str(exc))
        return redirect(url_for("execution_summary", run_id=run_id, job_id=job.job_id))

    @app.get("/runs/<run_id>/download/archive.zip")
    def download_archive(run_id: str):
        archive = _archive_or_404(output_root, run_id)
        return send_file(
            _zip_archive(archive["path"]),
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{run_id}.zip",
        )

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


def _artifact_groups(
    archive: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    labels = {
        "fact_import_payload.json": "Import payload",
        "fact_payload.json": "Legacy import payload",
        "api_readiness_report.json": "API readiness report",
        "fact_api_import_manifest.json": "Legacy API readiness report",
        "nsu_cleaned_review.xlsx": "NSU cleaned review workbook",
        "duplicate_forms_review.xlsx": "Duplicate form decision workbook",
        "address_verification_report.json": "Address verification report",
        "llm_actions_review.json": "LLM actions review report",
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
        "llm_actions_review.json",
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


def _run_factor_summary(archive: dict[str, Any]) -> dict[str, int]:
    """Summarise review contributors without mutating an immutable archive."""

    submissions = _load_json(archive["path"] / "submissions_cleaned.json", [])
    llm_factors = _llm_review_factors(archive)
    os_factors = _os_address_factors(archive)
    return {
        "unique_court_slug_count": len(
            {
                submission.get("court_slug")
                for submission in submissions
                if submission.get("court_slug")
            }
        ),
        "llm_review_submission_count": len({factor["source_row_number"] for factor in llm_factors}),
        "llm_review_issue_count": len(llm_factors),
        "os_action_blocking_submission_count": len(
            {factor["source_row_number"] for factor in os_factors}
        ),
        "os_action_blocking_address_count": len(os_factors),
    }


def _review_overview(archive: dict[str, Any], service: ApiExecutionService) -> dict[str, Any]:
    submissions = _load_json(archive["path"] / "submissions_cleaned.json", [])
    row_groups: dict[str, set[int]] = {}
    row_issue_counts: dict[str, int] = {}
    for submission in submissions:
        if submission.get("status") != "needs_human_review":
            continue
        row = submission.get("source", {}).get("source_row_number")
        if not isinstance(row, int):
            continue
        for issue in submission.get("issues", []):
            if issue.get("code") not in HUMAN_REVIEW_ISSUE_CODES and issue.get("severity") != "error":
                continue
            category = _review_category(
                str(issue.get("field") or ""), str(issue.get("code") or "")
            )
            row_groups.setdefault(category, set()).add(row)
            row_issue_counts[category] = row_issue_counts.get(category, 0) + 1

    run_id = archive["manifest"].get("run_id")
    llm = service.get_llm_actions_review(str(run_id))
    changes = service.get_api_changes_review(str(run_id))["changes"]
    hold_groups: dict[str, dict[str, Any]] = {}

    def add_hold(name: str, row: int | None, change_id: str) -> None:
        group = hold_groups.setdefault(name, {"rows": set(), "items": set()})
        if isinstance(row, int):
            group["rows"].add(row)
        group["items"].add(change_id)

    for item in llm.get("items", []):
        if item.get("approval_status") == "pending" and item.get("actionable"):
            add_hold("llm_approval", item.get("source_row_number"), str(item.get("review_id")))
    for change in changes:
        row = change.get("source_row_number")
        change_id = str(change.get("change_id"))
        if change.get("source_selection_required") and not change.get(
            "selected_source_row_number"
        ):
            add_hold("source_selection", row, change_id)
        comparison = change.get("comparison")
        if comparison is None:
            add_hold("target_not_checked", row, change_id)
        elif comparison.get("has_existing_data") and not comparison.get("is_no_change") and not change.get("target_approved"):
            add_hold("target_replacement", row, change_id)
        if change.get("action", {}).get("readiness") == "pending":
            reason = str(change.get("action", {}).get("reason") or "")
            add_hold("os_resolution" if "Address verification" in reason else "invalid_request", row, change_id)
        if change.get("execution_status") in {"blocked", "failed", "unknown"}:
            add_hold("execution_attention", row, change_id)

    return {
        "needs_review_rows": sum(
            submission.get("status") == "needs_human_review" for submission in submissions
        ),
        "row_categories": [
            {
                "code": category,
                "label": category.replace("_", " ").title(),
                "row_count": len(rows),
                "issue_count": row_issue_counts.get(category, 0),
            }
            for category, rows in sorted(row_groups.items(), key=lambda item: (-len(item[1]), item[0]))
        ],
        "hold_categories": [
            {
                "code": category,
                "label": category.replace("_", " ").title(),
                "row_count": len(values["rows"]),
                "item_count": len(values["items"]),
            }
            for category, values in sorted(
                hold_groups.items(), key=lambda item: (-len(item[1]["items"]), item[0])
            )
        ],
    }


def _review_category(field: str, code: str = "") -> str:
    if "DUPLICATE" in code or code == "COURT_SLUG_NOT_FOUND":
        return "court_identity_duplicates"
    root = field.split("[", 1)[0].split(".", 1)[0]
    return {
        "court_slug": "court_identity_duplicates",
        "addresses": "addresses",
        "contacts": "contacts",
        "facilities": "facilities_accessibility",
        "counter_service": "counter_service",
        "opening_hours": "opening_hours",
    }.get(root, "other")


def _submission_has_review_category(
    submission: dict[str, Any], category: str
) -> bool:
    return any(
        _review_category(str(issue.get("field") or ""), str(issue.get("code") or ""))
        == category
        and (
            issue.get("code") in HUMAN_REVIEW_ISSUE_CODES
            or issue.get("severity") == "error"
        )
        for issue in submission.get("issues", [])
    )


def _change_has_hold(change: dict[str, Any], hold: str) -> bool:
    comparison = change.get("comparison")
    if hold == "source_selection":
        return bool(
            change.get("source_selection_required")
            and not change.get("selected_source_row_number")
        )
    if hold == "target_not_checked":
        return comparison is None
    if hold == "target_replacement":
        return bool(
            comparison
            and comparison.get("has_existing_data")
            and not comparison.get("is_no_change")
            and not change.get("target_approved")
        )
    if hold == "invalid_request":
        reason = str(change.get("action", {}).get("reason") or "")
        return (
            change.get("action", {}).get("readiness") == "pending"
            and "Address verification" not in reason
        )
    if hold == "os_resolution":
        return (
            change.get("action", {}).get("readiness") == "pending"
            and "Address verification" in str(change.get("action", {}).get("reason") or "")
        )
    if hold == "execution_attention":
        return change.get("execution_status") in {"blocked", "failed", "unknown"}
    return True


def _llm_review_factors(archive: dict[str, Any]) -> list[dict[str, Any]]:
    """Return model-specific factors that directly hold a row for review."""

    submissions = _load_json(archive["path"] / "submissions_cleaned.json", [])
    factors: list[dict[str, Any]] = []
    for submission in submissions:
        if submission.get("status") != "needs_human_review":
            continue
        source_row_number = submission.get("source", {}).get("source_row_number")
        for issue in submission.get("issues", []):
            if issue.get("code") not in LLM_HUMAN_REVIEW_ISSUE_CODES:
                continue
            factors.append(
                {
                    "source_row_number": source_row_number,
                    "court_slug": submission.get("court_slug"),
                    "status": submission.get("status"),
                    "field": issue.get("field"),
                    "code": issue.get("code"),
                    "message": issue.get("message"),
                    "raw_value": issue.get("raw_value"),
                    "cleaned_value": issue.get("cleaned_value"),
                }
            )
    return sorted(
        factors,
        key=lambda factor: (
            factor["source_row_number"] is None,
            factor["source_row_number"] or 0,
            factor["field"] or "",
            factor["code"] or "",
        ),
    )


def _os_address_factors(archive: dict[str, Any]) -> list[dict[str, Any]]:
    """Return address actions held by FaCT/OS verification evidence."""

    report = _load_json(archive["path"] / "address_verification_report.json", {})
    verifications = report.get("verifications", []) if isinstance(report, dict) else []
    submissions = _load_json(archive["path"] / "submissions_cleaned.json", [])
    statuses_by_row = {
        submission.get("source", {}).get("source_row_number"): submission.get("status")
        for submission in submissions
    }
    factors = [
        {
            **verification,
            "record_status": statuses_by_row.get(verification.get("source_row_number")),
        }
        for verification in verifications
        if verification.get("status") in OS_ACTION_BLOCKING_STATUSES
    ]
    return sorted(
        factors,
        key=lambda factor: (
            factor.get("source_row_number") is None,
            factor.get("source_row_number") or 0,
            factor.get("address_index") or 0,
        ),
    )


def _zip_archive(archive_path: Path) -> BytesIO:
    """Build a download-only ZIP from the immutable run archive files."""

    archive_buffer = BytesIO()
    # The archive already contains XLSX files and large JSON outputs. Avoid
    # expensive recompression during a local browser request; the ZIP is a
    # convenient single-file container, not a storage optimisation.
    with ZipFile(archive_buffer, mode="w", compression=ZIP_STORED) as zip_file:
        for artifact in sorted(archive_path.iterdir()):
            if artifact.is_file():
                zip_file.write(artifact, arcname=artifact.name)
    archive_buffer.seek(0)
    return archive_buffer


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


def _review_confidence(item: dict[str, Any]) -> str:
    confidence = (item.get("model_result") or {}).get("confidence")
    return confidence if confidence in {"high", "medium", "low"} else "unavailable"


def _llm_review_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    rank = {"high": 0, "medium": 1, "low": 2, "unavailable": 3}
    return (
        rank[_review_confidence(item)],
        int(item.get("source_row_number") or 0),
        str(item.get("field") or ""),
        str(item.get("review_id") or ""),
    )


def _next_llm_review_url(
    service: ApiExecutionService,
    run_id: str,
    current_review_id: str,
    form: Any,
) -> str:
    """Return to the next matching pending decision without accepting an arbitrary URL."""

    status = str(form.get("review_status") or "")
    confidence = str(form.get("review_confidence") or "")
    query = str(form.get("review_query") or "")
    queue = str(form.get("review_queue") or "")
    folded_query = query.strip().casefold()
    payload = service.get_llm_actions_review(run_id)
    matching = sorted(
        [
            item
            for item in payload.get("items", [])
            if (not confidence or _review_confidence(item) == confidence)
            and (
                not folded_query
                or folded_query in str(item.get("court_slug") or "").casefold()
                or folded_query in str(item.get("source_row_number") or "").casefold()
                or folded_query in str(item.get("field") or "").casefold()
                or folded_query in str(item.get("review_id") or "").casefold()
            )
        ],
        key=_llm_review_sort_key,
    )
    ordered = [item for item in matching if item.get("kind") == "field"] + [
        item for item in matching if item.get("kind") == "address"
    ]
    current_index = next(
        (
            index
            for index, item in enumerate(ordered)
            if item.get("review_id") == current_review_id
        ),
        -1,
    )
    search_order = ordered[current_index + 1 :] + ordered[: current_index + 1]
    next_item = (
        next(
            (item for item in search_order if item.get("approval_status") == "pending"),
            None,
        )
        if status in {"", "pending"}
        else None
    )
    params = {
        "run_id": run_id,
        "status": status or None,
        "confidence": confidence or None,
        "q": query or None,
        "queue": queue or None,
        "field_page": form.get("field_page") or 1,
        "address_page": form.get("address_page") or 1,
    }
    if next_item is None:
        return url_for(
            "llm_actions_review",
            **params,
            complete=1 if status in {"", "pending"} else None,
            _anchor=(
                f"review-{current_review_id}"
                if status not in {"", "pending"}
                else None
            ),
        )

    visible = [
        item
        for item in matching
        if not status or item.get("approval_status") == status
    ]
    section = str(next_item.get("kind") or "field")
    section_items = [item for item in visible if item.get("kind") == section]
    target_index = next(
        index
        for index, item in enumerate(section_items)
        if item.get("review_id") == next_item.get("review_id")
    )
    params[f"{section}_page"] = target_index // PAGE_SIZE + 1
    return url_for(
        "llm_actions_review",
        **params,
        _anchor=f"review-{next_item['review_id']}",
    )


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
        "migration_assumptions": action.get("migration_assumptions") or [],
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
                (
                    item
                    for item in current
                    if isinstance(item, dict) and item.get("index") == int(part)
                ),
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
