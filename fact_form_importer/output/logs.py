"""Processing log and summary output generation."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fact_form_importer.ingest.workbook_reader import IngestResult
from fact_form_importer.ingest.workbook_profiler import WorkbookProfile
from fact_form_importer.models.court_submission import CourtSubmission
from fact_form_importer.output.fact_json import (
    build_fact_import_payload,
    build_failed_records,
    build_human_review_records,
)
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.validators.base import DUPLICATE_COURT_SLUG, LLM_HUMAN_REVIEW_ISSUE_CODES
from fact_form_importer.validators.vocabularies import Vocabularies


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
    llm_enabled: bool = False,
    llm_requested: bool = False,
    llm_metrics: dict[str, Any] | None = None,
    api_manifest_metrics: dict[str, int] | None = None,
    source_name: str | None = None,
    vocabularies: Vocabularies | None = None,
    court_lookup: Callable[[str], CourtReference | None] | None = None,
    address_verification_metrics: dict[str, Any] | None = None,
    submission_selection_metrics: dict[str, Any] | None = None,
) -> OutputResult:
    output_path.mkdir(parents=True, exist_ok=True)
    current_run_id = run_id or _new_run_id()

    fact_import_payload = build_fact_import_payload(
        submissions,
        run_id=current_run_id,
        vocabularies=vocabularies,
        court_lookup=court_lookup,
    )
    failed_records = build_failed_records(submissions)
    human_review_records = build_human_review_records(submissions)
    issue_report = build_issue_report(submissions)
    summary = build_import_summary(
        submissions=submissions,
        ingest_result=ingest_result,
        workbook_profile=workbook_profile,
        run_id=current_run_id,
        vocabulary_source=vocabulary_source,
        llm_enabled=llm_enabled,
        llm_requested=llm_requested,
        llm_metrics=llm_metrics,
        api_manifest_metrics=api_manifest_metrics,
        address_verification_metrics=address_verification_metrics,
        source_name=source_name,
        submission_selection_metrics=submission_selection_metrics,
    )

    _write_json(output_path / "fact_import_payload.json", fact_import_payload)
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
    llm_enabled: bool = False,
    llm_requested: bool = False,
    llm_metrics: dict[str, Any] | None = None,
    api_manifest_metrics: dict[str, int] | None = None,
    source_name: str | None = None,
    address_verification_metrics: dict[str, Any] | None = None,
    submission_selection_metrics: dict[str, Any] | None = None,
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
    unique_court_slugs = {submission.court_slug for submission in submissions if submission.court_slug}
    llm_review_submissions = [
        submission
        for submission in submissions
        if submission.status == "needs_human_review"
        and any(issue.code in LLM_HUMAN_REVIEW_ISSUE_CODES for issue in submission.issues)
    ]

    summary = {
        "run_id": run_id,
        "source_file": source_name or str(workbook_profile.source_path),
        "vocabulary_source": vocabulary_source,
        "llm_enabled": llm_enabled,
        "llm_requested": llm_requested,
        "row_count": workbook_profile.row_count,
        "submission_count": len(submissions),
        "unique_court_slug_count": len(unique_court_slugs),
        "status_count_total": sum(status_counts.values()),
        "llm_review_submission_count": len(llm_review_submissions),
        "llm_review_issue_count": sum(
            1
            for submission in llm_review_submissions
            for issue in submission.issues
            if issue.code in LLM_HUMAN_REVIEW_ISSUE_CODES
        ),
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
    summary.update(
        {
            "llm_calls": 0,
            "llm_failures": 0,
            "llm_retries": 0,
            "llm_fields_selected": 0,
            "llm_fields_processed": 0,
            "llm_submissions_with_selected_fields": 0,
            "llm_address_candidate_groups_selected": 0,
            "llm_address_suggestions_recorded": 0,
            "llm_model": None,
            "address_verification_enabled": False,
            "address_verification_count": 0,
            "address_verification_unique_postcode_lookups": 0,
            "address_verification_cache_hits": 0,
            "address_verification_rate_limit_retries": 0,
            "address_verification_auto_normalised_count": 0,
            "address_verification_verified_count": 0,
            "address_verification_review_required_count": 0,
            "address_verification_no_os_result_count": 0,
            "address_verification_invalid_postcode_count": 0,
            "address_verification_unsupported_postcode_region_count": 0,
            "address_verification_missing_postcode_count": 0,
            "address_verification_action_blocking_count": 0,
            "address_verification_action_blocking_submission_count": 0,
            "address_verification_unavailable_count": 0,
        }
    )
    if llm_metrics:
        summary.update(llm_metrics)
    if api_manifest_metrics:
        summary.update(api_manifest_metrics)
    if address_verification_metrics:
        summary.update(address_verification_metrics)
    if submission_selection_metrics:
        summary.update(
            {
                key: submission_selection_metrics[key]
                for key in (
                    "source_submission_count",
                    "authoritative_submission_count",
                    "duplicate_court_count",
                    "superseded_submission_count",
                    "duplicate_source_row_count",
                )
                if key in submission_selection_metrics
            }
        )
        summary["duplicate_slug_group_count"] = int(
            submission_selection_metrics.get("duplicate_court_count", 0)
        )
        summary["duplicate_slug_affected_record_count"] = int(
            submission_selection_metrics.get("duplicate_source_row_count", 0)
        )
    return summary


def new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _new_run_id() -> str:
    """Backward-compatible internal alias."""

    return new_run_id()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
