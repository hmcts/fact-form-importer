"""Processing log and summary output generation."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fact_form_importer.ingest.workbook_reader import IngestResult
from fact_form_importer.ingest.workbook_profiler import WorkbookProfile
from fact_form_importer.models.court_submission import CourtSubmission
from fact_form_importer.output.fact_json import (
    build_fact_payload,
    build_failed_records,
    build_human_review_records,
)
from fact_form_importer.validators.base import DUPLICATE_COURT_SLUG


@dataclass(frozen=True)
class OutputResult:
    run_id: str
    output_path: Path
    summary: dict[str, Any]


def write_processing_outputs(
    submissions: list[CourtSubmission],
    ingest_result: IngestResult,
    workbook_profile: WorkbookProfile,
    output_path: Path,
    run_id: str | None = None,
    vocabulary_source: str = "not_recorded",
) -> OutputResult:
    output_path.mkdir(parents=True, exist_ok=True)
    current_run_id = run_id or _new_run_id()

    fact_payload = build_fact_payload(submissions)
    failed_records = build_failed_records(submissions)
    human_review_records = build_human_review_records(submissions)
    issue_report = build_issue_report(submissions)
    summary = build_import_summary(
        submissions=submissions,
        ingest_result=ingest_result,
        workbook_profile=workbook_profile,
        run_id=current_run_id,
        vocabulary_source=vocabulary_source,
    )

    _write_json(output_path / "fact_payload.json", fact_payload)
    _write_json(output_path / "failed_records.json", failed_records)
    _write_json(output_path / "records_needing_human_review.json", human_review_records)
    _write_json(output_path / "issue_report.json", issue_report)
    _write_json(output_path / "import_summary.json", summary)

    return OutputResult(run_id=current_run_id, output_path=output_path, summary=summary)


def build_issue_report(submissions: list[CourtSubmission]) -> list[dict[str, Any]]:
    report = []
    for submission in submissions:
        for issue in submission.issues:
            report.append(
                {
                    "source_row_number": submission.source.source_row_number,
                    "court_slug": submission.court_slug,
                    "status": submission.status,
                    **issue.model_dump(mode="json"),
                }
            )
    return report


def build_import_summary(
    submissions: list[CourtSubmission],
    ingest_result: IngestResult,
    workbook_profile: WorkbookProfile,
    run_id: str,
    vocabulary_source: str = "not_recorded",
) -> dict[str, Any]:
    status_counts = Counter(submission.status for submission in submissions)
    issue_counts = Counter(
        issue.code
        for submission in submissions
        for issue in submission.issues
    )
    duplicate_groups = {
        submission.court_slug
        for submission in submissions
        if submission.court_slug
        and any(issue.code == DUPLICATE_COURT_SLUG for issue in submission.issues)
    }

    return {
        "run_id": run_id,
        "source_file": str(workbook_profile.source_path),
        "vocabulary_source": vocabulary_source,
        "row_count": workbook_profile.row_count,
        "submission_count": len(submissions),
        "processed_count": status_counts["processed"],
        "processed_with_warnings_count": status_counts["processed_with_warnings"],
        "needs_human_review_count": status_counts["needs_human_review"],
        "failed_count": status_counts["failed"],
        "skipped_count": ingest_result.skipped_empty_rows,
        "duplicate_slug_group_count": len(duplicate_groups),
        "duplicate_slug_affected_record_count": issue_counts[DUPLICATE_COURT_SLUG],
        "issue_counts_by_code": dict(sorted(issue_counts.items())),
        "mapping_warnings": ingest_result.mapping_warnings,
    }


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
