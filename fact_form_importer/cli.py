"""Command line interface for fact-form-importer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from fact_form_importer.config import AppConfig, load_default_field_rules
from fact_form_importer.execution.service import ApiExecutionService
from fact_form_importer.ingest.workbook_reader import ingest_workbook
from fact_form_importer.ingest.workbook_profiler import profile_to_json, profile_workbook
from fact_form_importer.llm.client import (
    build_llm_test_request,
    normalise_fields_with_llm,
)
from fact_form_importer.llm.pipeline import build_llm_request_review
from fact_form_importer.llm.prompts import SYSTEM_PROMPT
from fact_form_importer.llm.schemas import LlmNormalisationResponse
from fact_form_importer.llm.openai_client import check_llm_connection
from fact_form_importer.processing import load_fact_api_services, process_workbook


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

    serve_parser = subparsers.add_parser("serve", help="Start the localhost review UI.")
    serve_parser.add_argument("--output", required=True, type=Path, help="Importer output root containing final archives.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Localhost bind address.")
    serve_parser.add_argument("--port", default=5000, type=int, help="Localhost TCP port.")

    api_check_parser = subparsers.add_parser(
        "api-check-court", help="Preflight one reviewed court against existing FaCT API sections."
    )
    api_check_parser.add_argument("--output", required=True, type=Path, help="Importer output root.")
    api_check_parser.add_argument("--run-id", required=True, help="Archived run identifier.")
    api_check_parser.add_argument("--court-slug", required=True, help="Reviewed court slug.")

    api_action_parser = subparsers.add_parser(
        "api-execute-action", help="Execute one preflight-safe action for one court."
    )
    api_action_parser.add_argument("--output", required=True, type=Path, help="Importer output root.")
    api_action_parser.add_argument("--run-id", required=True, help="Archived run identifier.")
    api_action_parser.add_argument("--court-slug", required=True, help="Reviewed court slug.")
    api_action_parser.add_argument("--action-id", required=True, help="Action identifier from the readiness report.")
    api_action_parser.add_argument("--confirm", action="store_true", help="Required acknowledgement before any write.")

    api_court_parser = subparsers.add_parser(
        "api-execute-court", help="Execute all preflight-safe actions for one reviewed court."
    )
    api_court_parser.add_argument("--output", required=True, type=Path, help="Importer output root.")
    api_court_parser.add_argument("--run-id", required=True, help="Archived run identifier.")
    api_court_parser.add_argument("--court-slug", required=True, help="Reviewed court slug.")
    api_court_parser.add_argument("--confirm", action="store_true", help="Required acknowledgement before any write.")

    return parser


def run(
    input_path: Path,
    output_path: Path,
    allow_local_vocabularies: bool = False,
    use_llm: bool = False,
) -> int:
    try:
        result = process_workbook(
            input_path=input_path,
            output_root=output_path,
            allow_local_vocabularies=allow_local_vocabularies,
            use_llm=use_llm,
        )
    except (FileNotFoundError, KeyError, ModuleNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    summary = result.output.summary
    print(f"Run ID: {result.run_id}")
    print(f"Source file: {input_path}")
    print(f"Workbook rows: {summary['row_count']}")
    print(f"Validated submissions: {summary['submission_count']}")
    print(f"Processed: {summary['processed_count']}")
    print(f"Processed with warnings: {summary['processed_with_warnings_count']}")
    print(f"Needs human review: {summary['needs_human_review_count']}")
    print(f"Failed: {summary['failed_count']}")
    print(f"Duplicate court groups: {summary['duplicate_slug_group_count']}")
    print(
        "Duplicate affected records "
        f"(included in needs human review): {summary['duplicate_slug_affected_record_count']}"
    )
    print(f"Skipped empty rows: {summary['skipped_count']}")
    print(f"Read-only approval users: {result.submitters.user_count}")
    print(f"Excluded submitter users: {result.submitters.excluded_user_count}")
    print(f"LLM enabled: {summary['llm_enabled']}")
    print(f"LLM requested: {summary['llm_requested']}")
    print(f"LLM calls: {summary['llm_calls']}")
    print(f"LLM failures: {summary['llm_failures']}")
    print(f"LLM fields processed: {summary['llm_fields_processed']}")
    if summary["llm_model"]:
        print(f"LLM model: {summary['llm_model']}")
    print(f"Vocabulary source: {summary['vocabulary_source']}")
    print(f"API readiness ready actions: {summary['api_manifest_ready_action_count']}")
    print(f"API readiness pending actions: {summary['api_manifest_pending_action_count']}")
    print(f"Wrote NSU review workbook: {result.review_workbook_path}")
    print(f"Wrote read-only approval users: {result.submitters.json_path}")
    print(f"Archived run: {result.archive.archive_path}")
    print(f"Wrote latest run outputs to: {output_path}")
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
        vocabularies, vocabulary_source, _, _ = load_fact_api_services(
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


def api_check_court(output_path: Path, run_id: str, court_slug: str) -> int:
    try:
        ledger = ApiExecutionService(output_path).check_court(run_id, court_slug)
    except (FileNotFoundError, ValueError, ModuleNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    _print_api_execution_status(ledger, court_slug)
    print("No FaCT API write was made.")
    return 0


def api_execute_action(
    output_path: Path, run_id: str, court_slug: str, action_id: str, confirm: bool
) -> int:
    if not confirm:
        print("Error: --confirm is required before any FaCT API write", file=sys.stderr)
        return 1
    try:
        ledger = ApiExecutionService(output_path).execute_action(run_id, court_slug, action_id)
    except (FileNotFoundError, ValueError, ModuleNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    _print_api_execution_status(ledger, court_slug)
    return 0


def api_execute_court(output_path: Path, run_id: str, court_slug: str, confirm: bool) -> int:
    if not confirm:
        print("Error: --confirm is required before any FaCT API write", file=sys.stderr)
        return 1
    try:
        ledger = ApiExecutionService(output_path).execute_safe_court_actions(run_id, court_slug)
    except (FileNotFoundError, ValueError, ModuleNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    _print_api_execution_status(ledger, court_slug)
    return 0


def _print_api_execution_status(ledger, court_slug: str) -> None:
    court = ledger.courts.get(court_slug)
    if court is None:
        print(f"Court: {court_slug}")
        print("Execution status: not_started")
        return
    print(f"Court: {court_slug}")
    print(f"Execution status: {court.status}")
    for action_id, action in court.actions.items():
        suffix = f" ({action.reason})" if action.reason else ""
        print(f"- {action_id}: {action.status}{suffix}")


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

    if args.command == "serve":
        try:
            from fact_form_importer.web.app import run_server

            run_server(args.output, host=args.host, port=args.port)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "api-check-court":
        return api_check_court(args.output, args.run_id, args.court_slug)

    if args.command == "api-execute-action":
        return api_execute_action(
            args.output, args.run_id, args.court_slug, args.action_id, args.confirm
        )

    if args.command == "api-execute-court":
        return api_execute_court(args.output, args.run_id, args.court_slug, args.confirm)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
