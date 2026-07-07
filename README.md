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

## Workflow

### 1. Set up the project

Run the bootstrap script once per local checkout. It creates `.venv`, installs
the package with development dependencies, and installs the local pre-push hook
that runs unit tests before pushing.

```bash
sh scripts/bootstrap.sh
source .venv/bin/activate
```

### 2. Configure local environment

Copy the environment template and populate local values. `.env` is for local
secrets and machine-specific values, and is ignored by git. Keep `.env.example`
as a blank template.

```bash
cp .env.example .env
```

### 3. Add the source spreadsheet

Place the Microsoft Forms export in `input/`. Spreadsheet files in this
directory are ignored by git. Suggested filename:

```text
input/microsoft-forms-export.xlsx
```

### 4. Profile the spreadsheet

Profiling reads the spreadsheet without mutating it and reports the sheet name,
row count, column count, headers, empty counts, and sample values for each
column. This is a sanity check before building or running import processing
against a Microsoft Forms export.

It does not clean, validate, or transform the spreadsheet. It helps catch
problems early, such as the wrong source file, an unexpected number of rows or
columns, changed Microsoft Forms export layout, shifted headers, or columns
that are unexpectedly empty.

Run profiling when you first receive a spreadsheet, when the export format
changes, or when you want to confirm that the input file is the one you expect.
You do not need to run it every time if the spreadsheet format has already been
checked.

```bash
python3 -m fact_form_importer profile --input "./input/microsoft-forms-export.xlsx"
```

To also write `out/profile.json`:

```bash
python3 -m fact_form_importer profile --input "./input/microsoft-forms-export.xlsx" --output "./out"
```

### 5. Ingest the spreadsheet

Ingestion reads the source spreadsheet, applies deterministic cleaners, and
builds one `CourtSubmission` object per non-empty business row. It preserves
source metadata, raw values, cleaned values, repeated groups, and issues.

```bash
python3 -m fact_form_importer ingest --input "./input/microsoft-forms-export.xlsx" --output "./out"
```

This writes three intermediate files:

```text
out/submissions_raw.json
out/submissions_cleaned.json
out/ingest_summary.json
```

`submissions_raw.json` contains each ingested row with source metadata, the raw
spreadsheet values keyed by Excel column letter, row-level issues, and status.
This is mainly an audit/debug file for tracing a cleaned value back to the
original spreadsheet.

`submissions_cleaned.json` contains the structured `CourtSubmission` records
after deterministic cleaning. It includes cleaned facilities, addresses,
counter service, interview rooms, contact details, opening hours, issues, and
status. This is still an intermediate processing artifact, not the final FaCT
API payload.

`ingest_summary.json` contains counts for ingested submissions, skipped empty
rows, failed rows, warning rows, and mapping warnings. Use it as the first check
that ingestion behaved as expected.

Later steps will use these `CourtSubmission` records for field-rule validation,
vocabulary normalisation, optional LLM-assisted cleanup, NSU review workbook
generation, and final `fact_payload.json` creation.

### 6. Run the full importer

The `run` command is currently a placeholder. Later it will process the
spreadsheet and write import outputs:

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
