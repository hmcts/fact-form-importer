"""Command line interface for fact-form-importer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from fact_form_importer.config import AppConfig
from fact_form_importer.ingest.workbook_reader import ingest_workbook
from fact_form_importer.ingest.workbook_profiler import profile_to_json, profile_workbook
from fact_form_importer.output.logs import write_processing_outputs
from fact_form_importer.output.nsu_workbook import write_nsu_review_workbook
from fact_form_importer.output.submitters import write_submitter_outputs
from fact_form_importer.validators.fact_api_courts import (
    court_slug_exists_in_fact_api,
    suggest_court_slug_in_fact_api,
)
from fact_form_importer.validators.fact_api_vocabularies import load_vocabularies_from_fact_api
from fact_form_importer.validators.business_rules import validate_all_submissions
from fact_form_importer.validators.vocabularies import load_vocabularies


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fact-form-importer",
        description="Process Microsoft Forms court exports for FaCT import review.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Process a Microsoft Forms export.")
    run_parser.add_argument("--input", required=True, type=Path, help="Path to the XLSX or CSV export.")
    run_parser.add_argument("--output", required=True, type=Path, help="Directory for generated outputs.")
    run_parser.add_argument(
        "--allow-local-vocabularies",
        action="store_true",
        help=(
            "Allow config/vocabularies.example.json when FaCT Data API vocabulary loading "
            "is unavailable. Intended for local/offline review only."
        ),
    )

    profile_parser = subparsers.add_parser("profile", help="Profile a Microsoft Forms export.")
    profile_parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to the XLSX or CSV export.",
    )
    profile_parser.add_argument(
        "--output",
        type=Path,
        help="Directory to write profile.json.",
    )

    ingest_parser = subparsers.add_parser("ingest", help="Ingest a Microsoft Forms export.")
    ingest_parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to the XLSX or CSV export.",
    )
    ingest_parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Directory for generated ingest outputs.",
    )

    return parser


def run(input_path: Path, output_path: Path, allow_local_vocabularies: bool = False) -> int:
    try:
        config = AppConfig()
        workbook_profile = profile_workbook(input_path)
        ingest_result = ingest_workbook(input_path=input_path, output_path=output_path)
        vocabularies, vocabulary_source, court_slug_exists, court_slug_suggester = _load_fact_api_services_for_run(
            config=config,
            allow_local_vocabularies=allow_local_vocabularies
        )
        submissions = validate_all_submissions(
            ingest_result.submissions,
            vocabularies,
            court_slug_exists=court_slug_exists,
            court_slug_suggester=court_slug_suggester,
        )
        output_path.mkdir(parents=True, exist_ok=True)
        (output_path / "profile.json").write_text(
            profile_to_json(workbook_profile) + "\n",
            encoding="utf-8",
        )
        output_result = write_processing_outputs(
            submissions=submissions,
            ingest_result=ingest_result,
            workbook_profile=workbook_profile,
            output_path=output_path,
            vocabulary_source=vocabulary_source,
            llm_enabled=config.llm_enabled,
        )
        workbook_path = write_nsu_review_workbook(
            submissions=submissions,
            output_path=output_path,
            summary=output_result.summary,
        )
        submitter_result = write_submitter_outputs(
            submissions=submissions,
            output_path=output_path,
        )
    except (FileNotFoundError, KeyError, ModuleNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    summary = output_result.summary
    print(f"Run ID: {output_result.run_id}")
    print(f"Source file: {input_path}")
    print(f"Workbook rows: {summary['row_count']}")
    print(f"Validated submissions: {summary['submission_count']}")
    print(f"Processed: {summary['processed_count']}")
    print(f"Processed with warnings: {summary['processed_with_warnings_count']}")
    print(f"Needs human review: {summary['needs_human_review_count']}")
    print(f"Failed: {summary['failed_count']}")
    print(f"Duplicate court groups: {summary['duplicate_slug_group_count']}")
    print(f"Duplicate affected records: {summary['duplicate_slug_affected_record_count']}")
    print(f"Skipped empty rows: {summary['skipped_count']}")
    print(f"Read-only approval users: {submitter_result.user_count}")
    print(f"Excluded submitter users: {submitter_result.excluded_user_count}")
    print(f"LLM enabled: {summary['llm_enabled']}")
    print(f"Vocabulary source: {summary['vocabulary_source']}")
    print(f"Wrote NSU review workbook: {workbook_path}")
    print(f"Wrote read-only approval users: {submitter_result.json_path}")
    print(f"Wrote run outputs to: {output_path}")
    return 0


def profile(input_path: Path, output_path: Optional[Path] = None) -> int:
    try:
        workbook_profile = profile_workbook(input_path)
    except (FileNotFoundError, KeyError, ModuleNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Workbook profile: {workbook_profile.source_path}")
    if workbook_profile.sheet_name:
        print(f"Sheet: {workbook_profile.sheet_name}")
    print(f"Rows: {workbook_profile.row_count}")
    print(f"Columns: {workbook_profile.column_count}")
    print("")
    print("Columns:")

    for column in workbook_profile.columns:
        header = "" if column.header is None else str(column.header)
        print(
            f"  {column.excel_letter} ({column.index}): {header} "
            f"- non-empty {column.non_empty_count}, empty {column.empty_count}"
        )

    if output_path:
        output_path.mkdir(parents=True, exist_ok=True)
        profile_path = output_path / "profile.json"
        profile_path.write_text(profile_to_json(workbook_profile) + "\n", encoding="utf-8")
        print("")
        print(f"Wrote profile JSON: {profile_path}")

    return 0


def ingest(input_path: Path, output_path: Path) -> int:
    try:
        result = ingest_workbook(input_path=input_path, output_path=output_path)
    except (FileNotFoundError, KeyError, ModuleNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Ingested submissions: {len(result.submissions)}")
    print(f"Skipped empty rows: {result.skipped_empty_rows}")
    print(f"Mapping warnings: {len(result.mapping_warnings)}")
    print(f"Wrote ingest outputs to: {output_path}")
    return 0


def _load_fact_api_services_for_run(
    config: AppConfig | None = None,
    allow_local_vocabularies: bool = False,
):
    config = config or AppConfig()
    path = config.vocabularies_path
    local_vocabularies = load_vocabularies(path) if path.exists() else None

    if not config.fact_data_api_base_url:
        if allow_local_vocabularies:
            return local_vocabularies, "local_json" if local_vocabularies else "none", None, None
        raise ValueError(
            "FACT_DATA_API_BASE_URL is required for run. "
            "Use --allow-local-vocabularies only for offline/local review."
        )

    if not config.fact_data_api_bearer_token:
        if allow_local_vocabularies:
            return local_vocabularies, "local_json" if local_vocabularies else "none", None, None
        raise ValueError(
            "FACT_DATA_API_BEARER_TOKEN is required for run. "
            "Use --allow-local-vocabularies only for offline/local review."
        )

    def court_slug_exists(court_slug: str) -> bool:
        try:
            return court_slug_exists_in_fact_api(
                court_slug=court_slug,
                base_url=config.fact_data_api_base_url or "",
                bearer_token=config.fact_data_api_bearer_token or "",
            )
        except Exception as exc:
            raise ValueError(f"Unable to validate court slug '{court_slug}' against FaCT API: {exc}") from exc

    def court_slug_suggester(court_slug: str, raw_value: str | None):
        try:
            return suggest_court_slug_in_fact_api(
                court_slug=court_slug,
                raw_value=raw_value,
                base_url=config.fact_data_api_base_url or "",
                bearer_token=config.fact_data_api_bearer_token or "",
            )
        except Exception as exc:
            raise ValueError(f"Unable to suggest court slug for '{court_slug}' against FaCT API: {exc}") from exc

    try:
        return (
            load_vocabularies_from_fact_api(
                base_url=config.fact_data_api_base_url,
                bearer_token=config.fact_data_api_bearer_token,
                fallback=local_vocabularies,
            ),
            "fact_data_api",
            court_slug_exists,
            court_slug_suggester,
        )
    except Exception as exc:
        if not allow_local_vocabularies or local_vocabularies is None:
            raise ValueError(f"Unable to load FaCT API vocabularies: {exc}") from exc
        print(f"Warning: using local vocabularies because FaCT API lookup failed: {exc}", file=sys.stderr)
        return local_vocabularies, "local_json_fallback_after_fact_data_api_error", None, None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return run(
            args.input,
            args.output,
            allow_local_vocabularies=args.allow_local_vocabularies,
        )

    if args.command == "profile":
        return profile(args.input, args.output)

    if args.command == "ingest":
        return ingest(args.input, args.output)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
