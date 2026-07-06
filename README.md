# fact-form-importer

Processes Microsoft Forms XLSX/CSV exports containing Find a Court or Tribunal
court information. The importer will convert spreadsheet rows into structured,
cleaned, validated data for later FaCT API import.

The project is intentionally incremental. The initial skeleton contains package
structure, configuration placeholders, and a CLI entry point. Processing logic
will be added in later tasks.

## Goals

- Read Microsoft Forms XLSX/CSV court submission exports.
- Create one `CourtSubmission` object per spreadsheet row.
- Preserve raw values, cleaned values, and validation issues.
- Use deterministic Python cleaners before any LLM-assisted normalisation.
- Use Azure OpenAI GPT-5.5 only for configured ambiguous fields.
- Keep Python responsible for the final FaCT payload shape.
- Generate import JSON, NSU review workbook, summary logs, issue reports, and
  read-only approval user outputs.

## Development

Run the bootstrap script once from the repo root:

```bash
sh scripts/bootstrap.sh
```

This creates `.venv`, installs the package with development dependencies, and
installs the local pre-push hook that runs unit tests before pushing.

Activate the virtual environment when working in the repo:

```bash
source .venv/bin/activate
```

## Local Configuration

Copy `.env.example` to `.env` and populate the local values:

```bash
cp .env.example .env
```

Do not commit `.env` or real secrets. `.env.example` is a template only: keep
values empty or use obvious placeholders. The test suite checks that
`.env.example` does not contain real values.

Place local source spreadsheets in `input/`. A suggested filename is:

```text
input/microsoft-forms-export.xlsx
```

The `input/` directory is for local working files only. Spreadsheet exports in
that directory are ignored by git.

## CLI

The initial CLI parses input and output paths and prints a placeholder message:

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out"
```

## Project Layout

```text
fact_form_importer/
  cli.py
  config.py
  ingest/
  models/
  cleaners/
  validators/
  llm/
  output/
config/
tests/
```
