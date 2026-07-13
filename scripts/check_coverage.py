#!/usr/bin/env python3
"""Fail if coverage JSON is below configured exact percentages."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path


CORE_GROUPS = {
    "cleaners": ("fact_form_importer/cleaners/",),
    "ingest_core": (
        "fact_form_importer/ingest/column_mapping.py",
        "fact_form_importer/ingest/workbook_reader.py",
    ),
    "validators": (
        "fact_form_importer/validators/base.py",
        "fact_form_importer/validators/business_rules.py",
        "fact_form_importer/validators/duplicates.py",
        "fact_form_importer/validators/vocabularies.py",
    ),
    "api_manifest": (
        "fact_form_importer/processing.py",
        "fact_form_importer/output/archive.py",
        "fact_form_importer/output/fact_api_manifest.py",
    ),
    "web": ("fact_form_importer/web/",),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check exact coverage percentages.")
    parser.add_argument("coverage_json", type=Path, help="Path to coverage.py JSON output.")
    parser.add_argument("--fail-under", type=Decimal, required=True, help="Minimum total coverage.")
    parser.add_argument(
        "--core-fail-under",
        type=Decimal,
        help="Minimum coverage for deterministic core groups.",
    )
    args = parser.parse_args()

    data = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    failures = []

    total = Decimal(str(data["totals"]["percent_covered"]))
    if total < args.fail_under:
        failures.append(f"Coverage {total:.2f}% is below required {args.fail_under:.2f}%")
    else:
        print(f"Coverage {total:.2f}% meets required {args.fail_under:.2f}%")

    if args.core_fail_under is not None:
        for group_name, path_prefixes in CORE_GROUPS.items():
            group_coverage = _group_coverage(data, path_prefixes)
            if group_coverage < args.core_fail_under:
                failures.append(
                    f"{group_name} coverage {group_coverage:.2f}% is below required "
                    f"{args.core_fail_under:.2f}%"
                )
            else:
                print(
                    f"{group_name} coverage {group_coverage:.2f}% meets required "
                    f"{args.core_fail_under:.2f}%"
                )

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1

    return 0


def _group_coverage(data: dict, path_prefixes: tuple[str, ...]) -> Decimal:
    covered_lines = Decimal("0")
    statements = Decimal("0")
    covered_branches = Decimal("0")
    branches = Decimal("0")

    for path, file_data in data["files"].items():
        if not any(path == prefix or path.startswith(prefix) for prefix in path_prefixes):
            continue

        summary = file_data["summary"]
        covered_lines += Decimal(str(summary["covered_lines"]))
        statements += Decimal(str(summary["num_statements"]))
        covered_branches += Decimal(str(summary.get("covered_branches", 0)))
        branches += Decimal(str(summary.get("num_branches", 0)))

    denominator = statements + branches
    if denominator == 0:
        return Decimal("100")

    return ((covered_lines + covered_branches) / denominator) * Decimal("100")


if __name__ == "__main__":
    raise SystemExit(main())
