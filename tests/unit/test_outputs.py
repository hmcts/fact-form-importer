import json

from fact_form_importer.ingest.workbook_profiler import WorkbookProfile
from fact_form_importer.ingest.workbook_reader import IngestResult
from fact_form_importer.models.court_submission import CourtSubmission
from fact_form_importer.models.court_submission import OpeningTime
from fact_form_importer.models.issues import Issue
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.output.fact_json import (
    build_fact_payload,
    build_failed_records,
    build_human_review_records,
)
from fact_form_importer.output.logs import build_import_summary, write_processing_outputs


def test_build_fact_payload_excludes_failed_and_human_review_records():
    submissions = [
        _submission("processed-court", "processed"),
        _submission("warning-court", "processed_with_warnings"),
        _submission("review-court", "needs_human_review"),
        _submission("failed-court", "failed"),
    ]

    payload = build_fact_payload(submissions)

    assert [record["court_slug"] for record in payload] == [
        "processed-court",
        "warning-court",
    ]


def test_build_fact_payload_serialises_nested_models_in_dict_fields():
    submission = _submission("processed-court", "processed")
    submission.counter_service = {
        "monday_to_friday": OpeningTime(open="09:00", close="17:00", status="valid_time")
    }

    payload = build_fact_payload([submission])

    assert payload[0]["counter_service"]["monday_to_friday"]["open"] == "09:00"


def test_review_output_builders_include_only_matching_statuses():
    failed = _submission("failed-court", "failed")
    review = _submission("review-court", "needs_human_review")
    processed = _submission("processed-court", "processed")

    assert [record["court_slug"] for record in build_failed_records([failed, review, processed])] == [
        "failed-court"
    ]
    assert [
        record["court_slug"]
        for record in build_human_review_records([failed, review, processed])
    ] == ["review-court"]


def test_build_import_summary_counts_statuses_and_issues(tmp_path):
    submissions = [
        _submission("processed-court", "processed"),
        _submission("warning-court", "processed_with_warnings", issue_code="INVALID_PHONE"),
        _submission("review-court", "needs_human_review", issue_code="DUPLICATE_COURT_SLUG"),
        _submission("failed-court", "failed", issue_code="MISSING_COURT_IDENTIFIER"),
    ]
    ingest_result = IngestResult(submissions=submissions, skipped_empty_rows=3)
    profile = _profile(tmp_path / "source.csv", row_count=7)

    summary = build_import_summary(
        submissions,
        ingest_result,
        profile,
        "run-1",
        vocabulary_source="fact_data_api",
    )

    assert summary["run_id"] == "run-1"
    assert summary["source_file"].endswith("source.csv")
    assert summary["vocabulary_source"] == "fact_data_api"
    assert summary["row_count"] == 7
    assert summary["submission_count"] == 4
    assert summary["processed_count"] == 1
    assert summary["processed_with_warnings_count"] == 1
    assert summary["needs_human_review_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["skipped_count"] == 3
    assert summary["duplicate_slug_group_count"] == 1
    assert summary["duplicate_slug_affected_record_count"] == 1
    assert summary["issue_counts_by_code"]["INVALID_PHONE"] == 1


def test_write_processing_outputs_writes_expected_files(tmp_path):
    submissions = [
        _submission("processed-court", "processed"),
        _submission("failed-court", "failed", issue_code="MISSING_COURT_IDENTIFIER"),
    ]
    ingest_result = IngestResult(submissions=submissions, skipped_empty_rows=1)
    profile = _profile(tmp_path / "source.csv", row_count=4)

    result = write_processing_outputs(
        submissions=submissions,
        ingest_result=ingest_result,
        workbook_profile=profile,
        output_path=tmp_path,
        run_id="run-1",
        vocabulary_source="local_json",
    )

    assert result.run_id == "run-1"
    assert (tmp_path / "fact_payload.json").exists()
    assert (tmp_path / "failed_records.json").exists()
    assert (tmp_path / "records_needing_human_review.json").exists()
    assert (tmp_path / "issue_report.json").exists()
    assert (tmp_path / "import_summary.json").exists()

    payload = json.loads((tmp_path / "fact_payload.json").read_text())
    summary = json.loads((tmp_path / "import_summary.json").read_text())
    assert payload[0]["court_slug"] == "processed-court"
    assert summary["failed_count"] == 1
    assert summary["vocabulary_source"] == "local_json"


def _submission(court_slug, status, issue_code=None):
    issues = []
    if issue_code:
        issues.append(
            Issue(
                field="court_slug",
                code=issue_code,
                severity="warning",
                message="Test issue",
            )
        )

    return CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug_raw=court_slug,
        court_slug=court_slug,
        status=status,
        issues=issues,
    )


def _profile(source_path, row_count):
    return WorkbookProfile(
        source_path=source_path,
        sheet_name=None,
        row_count=row_count,
        column_count=0,
        columns=[],
    )
