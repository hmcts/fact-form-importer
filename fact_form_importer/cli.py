"""Command line interface for fact-form-importer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from fact_form_importer.ingest.workbook_profiler import profile_to_json, profile_workbook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fact-form-importer",
        description="Process Microsoft Forms court exports for FaCT import review.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Process a Microsoft Forms export.")
    run_parser.add_argument("--input", required=True, type=Path, help="Path to the XLSX or CSV export.")
    run_parser.add_argument("--output", required=True, type=Path, help="Directory for generated outputs.")

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

    return parser


def run(input_path: Path, output_path: Path) -> int:
    print(f"fact-form-importer placeholder: input={input_path} output={output_path}")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return run(args.input, args.output)

    if args.command == "profile":
        return profile(args.input, args.output)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
