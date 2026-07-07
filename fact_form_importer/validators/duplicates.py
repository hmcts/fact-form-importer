"""Duplicate submission detection."""

from __future__ import annotations

from collections import defaultdict

from fact_form_importer.models.court_submission import CourtSubmission
from fact_form_importer.models.issues import Issue

DUPLICATE_COURT_SLUG = "DUPLICATE_COURT_SLUG"


def flag_duplicate_court_slugs(submissions: list[CourtSubmission]) -> None:
    by_slug: dict[str, list[CourtSubmission]] = defaultdict(list)
    for submission in submissions:
        if submission.court_slug:
            by_slug[submission.court_slug].append(submission)

    for court_slug, matching_submissions in by_slug.items():
        if len(matching_submissions) < 2:
            continue

        rows = [
            submission.source.source_row_number
            for submission in matching_submissions
        ]
        for submission in matching_submissions:
            if any(issue.code == DUPLICATE_COURT_SLUG for issue in submission.issues):
                continue
            submission.issues.append(
                Issue(
                    field="court_slug",
                    code=DUPLICATE_COURT_SLUG,
                    severity="warning",
                    message="Duplicate court slug appears in multiple submissions",
                    raw_value=court_slug,
                    cleaned_value={"source_row_numbers": rows},
                )
            )
