from fact_form_importer.models.court_submission import CourtSubmission
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.output.duplicate_review import select_authoritative_submissions


def _submission(row: int, slug: str, **timestamps: str) -> CourtSubmission:
    return CourtSubmission(
        source=SourceMetadata(source_row_number=row, **timestamps),
        court_slug=slug,
    )


def test_latest_completed_submission_is_authoritative_and_older_rows_are_skipped():
    older = _submission(2, "duplicate-court", completion_time="2026-01-01T12:00:00")
    latest = _submission(3, "duplicate-court", completion_time="2026-01-02T12:00:00")
    unique = _submission(4, "unique-court")

    authoritative, evidence = select_authoritative_submissions([older, latest, unique])

    assert [submission.source.source_row_number for submission in authoritative] == [3, 4]
    assert older.selection_status == "superseded"
    assert older.superseded_by_source_row_number == 3
    assert older.status == "skipped"
    assert evidence["duplicate_court_count"] == 1
    assert evidence["superseded_source_row_numbers"] == [2]


def test_latest_selection_uses_modified_start_and_row_fallbacks_deterministically():
    modified_old = _submission(2, "modified", last_modified_time="2026-01-01T12:00:00")
    modified_new = _submission(3, "modified", last_modified_time="2026-01-02T12:00:00")
    start_old = _submission(4, "started", start_time="2026-01-01T12:00:00")
    start_new = _submission(5, "started", start_time="2026-01-02T12:00:00")
    no_date_old = _submission(6, "undated")
    no_date_new = _submission(7, "undated")

    authoritative, evidence = select_authoritative_submissions(
        [modified_old, modified_new, start_old, start_new, no_date_old, no_date_new]
    )

    assert {submission.source.source_row_number for submission in authoritative} == {3, 5, 7}
    assert evidence["superseded_source_row_numbers"] == [2, 4, 6]
