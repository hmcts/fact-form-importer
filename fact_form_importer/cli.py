"""Command line interface for fact-form-importer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from fact_form_importer.config import AppConfig, load_default_field_rules
from fact_form_importer.ingest.workbook_reader import ingest_workbook
from fact_form_importer.ingest.workbook_profiler import profile_to_json, profile_workbook
from fact_form_importer.llm.client import (
    build_llm_test_request,
    normalise_fields_with_llm,
    validate_openai_config,
)
from fact_form_importer.llm.pipeline import (
    LlmUsageMetrics,
    build_llm_request_review,
    normalise_submissions_with_llm,
)
from fact_form_importer.llm.prompts import SYSTEM_PROMPT
from fact_form_importer.llm.schemas import LlmNormalisationResponse
from fact_form_importer.llm.openai_client import check_llm_connection
from fact_form_importer.output.logs import write_processing_outputs
from fact_form_importer.output.nsu_workbook import write_nsu_review_workbook
from fact_form_importer.output.submitters import write_submitter_outputs
from fact_form_importer.validators.fact_api_courts import (
    court_slug_exists_in_fact_api,
    suggest_court_slug_in_fact_api,
)
from fact_form_importer.validators.fact_api_vocabularies import load_vocabularies_from_fact_api
from fact_form_importer.validators.business_rules import validate_all_submissions
from fact_form_importer.validators.base import clear_validation_issues
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

    llm_review_parser = subparsers.add_parser(
        "llm-request-review",
        help="Write the safe LLM request payloads without calling the model.",
    )
    llm_review_parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to the XLSX or CSV export.",
    )
    llm_review_parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Directory for llm_request_review.json.",
    )
    llm_review_parser.add_argument(
        "--allow-local-vocabularies",
        action="store_true",
        help="Allow local vocabulary fixtures when FaCT Data API access is unavailable.",
    )
    run_parser.add_argument(
        "--use-llm",
        action="store_true",
        help=(
            "Apply optional LLM normalisation to selected safe fields. Requires "
            "LLM_ENABLED=true and configured OpenAI settings."
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

    subparsers.add_parser("check-llm", help="Check the configured OpenAI-compatible LLM endpoint.")
    subparsers.add_parser("llm-test", help="Send a tiny fake structured LLM normalisation request.")

    return parser


def run(
    input_path: Path,
    output_path: Path,
    allow_local_vocabularies: bool = False,
    use_llm: bool = False,
) -> int:
    try:
        config = AppConfig()
        if use_llm and not config.llm_enabled:
            raise ValueError("--use-llm requires LLM_ENABLED=true in .env")
        if use_llm:
            validate_openai_config(config, command_name="run --use-llm")
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
        llm_metrics = LlmUsageMetrics()
        if use_llm:
            llm_result = normalise_submissions_with_llm(
                submissions,
                field_rules=load_default_field_rules(config),
                vocabularies=vocabularies,
                config=config,
            )
            submissions = llm_result.submissions
            llm_metrics = llm_result.metrics
            clear_validation_issues(submissions)
            submissions = validate_all_submissions(
                submissions,
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
            llm_requested=use_llm,
            llm_metrics=llm_metrics.as_dict(config.openai_model if use_llm else None),
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
    print(f"LLM requested: {summary['llm_requested']}")
    print(f"LLM calls: {summary['llm_calls']}")
    print(f"LLM failures: {summary['llm_failures']}")
    print(f"LLM fields processed: {summary['llm_fields_processed']}")
    if summary["llm_model"]:
        print(f"LLM model: {summary['llm_model']}")
    print(f"Vocabulary source: {summary['vocabulary_source']}")
    print(f"Wrote NSU review workbook: {workbook_path}")
    print(f"Wrote read-only approval users: {submitter_result.json_path}")
    print(f"Wrote run outputs to: {output_path}")
    return 0


def llm_request_review(
    input_path: Path,
    output_path: Path,
    allow_local_vocabularies: bool = False,
) -> int:
    """Write exact safe request payloads for inspection without model calls."""

    try:
        config = AppConfig()
        ingest_result = ingest_workbook(input_path=input_path)
        vocabularies, vocabulary_source, _, _ = _load_fact_api_services_for_run(
            config=config,
            allow_local_vocabularies=allow_local_vocabularies,
        )
        requests = build_llm_request_review(
            ingest_result.submissions,
            field_rules=load_default_field_rules(config),
            vocabularies=vocabularies,
        )
        payload = {
            "source_file": str(input_path),
            "model": config.openai_model,
            "llm_enabled": config.llm_enabled,
            "model_calls_made": 0,
            "vocabulary_source": vocabulary_source,
            "request_count": len(requests),
            "field_count": sum(len(request.fields) for request in requests),
            "instructions": SYSTEM_PROMPT,
            "response_schema": LlmNormalisationResponse.model_json_schema(),
            "requests": [request.model_dump(mode="json", exclude_none=True) for request in requests],
        }
        output_path.mkdir(parents=True, exist_ok=True)
        review_path = output_path / "llm_request_review.json"
        review_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except (FileNotFoundError, KeyError, ModuleNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"LLM request review records: {payload['request_count']}")
    print(f"LLM request review fields: {payload['field_count']}")
    print("LLM calls made: 0")
    print(f"Vocabulary source: {payload['vocabulary_source']}")
    print(f"Wrote LLM request review: {review_path}")
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


def check_llm() -> int:
    try:
        config = AppConfig()
        result = check_llm_connection(config)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("LLM connection: OK")
    print(f"LLM enabled: {config.llm_enabled}")
    print(f"OpenAI base URL: {result.base_url}")
    print(f"OpenAI model: {result.model}")
    print(f"Response preview: {result.output_preview}")
    return 0


def llm_test() -> int:
    try:
        config = AppConfig()
        request = build_llm_test_request()
        response = normalise_fields_with_llm(request, config)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("LLM normalisation test: OK")
    print("LLM called by this command: True")
    print(f"Pipeline LLM enabled for run: {config.llm_enabled}")
    print(f"OpenAI model: {config.openai_model}")
    print("")
    _print_llm_test_input(request)
    print("")
    _print_llm_test_output(response)
    return 0


def _print_llm_test_input(request) -> None:
    print("Input fields:")
    for field in request.fields:
        print(f"- {field.field}")
        print(f"  raw: {_display_value(field.raw_value)}")
        print(f"  cleaned: {_display_value(field.cleaned_value)}")


def _print_llm_test_output(response) -> None:
    print("Output fields:")
    for field in response.normalised_fields:
        print(f"- {field.field}")
        print(f"  value: {_display_value(field.value)}")
        print(f"  confidence: {field.confidence}")
        print(f"  needs_human_review: {field.needs_human_review}")
        print(f"  reason: {field.reason}")

    print("")
    print("Issues:")
    if response.issues:
        for issue in response.issues:
            print(f"- {issue.field} [{issue.severity}] {issue.code}: {issue.message}")
    else:
        print("- None")

    print("")
    print("Result:")
    print(f"confidence: {response.confidence}")
    print(f"needs_human_review: {response.needs_human_review}")


def _display_value(value) -> str:
    if value is None:
        return "<null>"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "[]"
    return str(value)


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
            use_llm=args.use_llm,
        )

    if args.command == "profile":
        return profile(args.input, args.output)

    if args.command == "ingest":
        return ingest(args.input, args.output)

    if args.command == "check-llm":
        return check_llm()

    if args.command == "llm-test":
        return llm_test()

    if args.command == "llm-request-review":
        return llm_request_review(
            args.input,
            args.output,
            allow_local_vocabularies=args.allow_local_vocabularies,
        )

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
