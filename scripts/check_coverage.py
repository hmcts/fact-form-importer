#!/usr/bin/env python3
"""Fail if coverage JSON is below the required exact percentage."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Check exact coverage percentage.")
    parser.add_argument("coverage_json", type=Path, help="Path to coverage.py JSON output.")
    parser.add_argument("--fail-under", type=Decimal, required=True, help="Minimum required coverage.")
    args = parser.parse_args()

    data = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    actual = Decimal(str(data["totals"]["percent_covered"]))

    if actual < args.fail_under:
        print(
            f"Coverage {actual:.2f}% is below required {args.fail_under:.2f}%",
            file=sys.stderr,
        )
        return 1

    print(f"Coverage {actual:.2f}% meets required {args.fail_under:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
