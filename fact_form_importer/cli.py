"""Command line interface for fact-form-importer."""

from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fact-form-importer",
        description="Process Microsoft Forms court exports for FaCT import review.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Process a Microsoft Forms export.")
    run_parser.add_argument("--input", required=True, type=Path, help="Path to the XLSX or CSV export.")
    run_parser.add_argument("--output", required=True, type=Path, help="Directory for generated outputs.")

    return parser


def run(input_path: Path, output_path: Path) -> int:
    print(f"fact-form-importer placeholder: input={input_path} output={output_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return run(args.input, args.output)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
