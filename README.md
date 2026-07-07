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
that runs unit tests with coverage before pushing.

```bash
sh scripts/bootstrap.sh
source .venv/bin/activate
```

Run the unit suite with the same coverage threshold used by the pre-push hook:

```bash
python3 -m pytest tests/unit --cov=fact_form_importer --cov-report=term-missing
```

Coverage is configured to fail below 90%.

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

The `run` command performs the current end-to-end non-LLM pipeline:
profile the workbook, ingest rows, validate submissions, calculate statuses,
and write draft import/review JSON outputs.

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out"
```

This also writes `profile.json` and the ingest intermediate files listed above.
It then writes:

```text
out/fact_payload.json
out/failed_records.json
out/records_needing_human_review.json
out/issue_report.json
out/import_summary.json
```

`fact_payload.json` contains only records with status `processed` or
`processed_with_warnings`, provided they have no blocking error issues. The
shape is intentionally inspectable and draft-like; it is not the final FaCT API
request body yet.

`failed_records.json` contains records that cannot progress because required
data is missing or schema-critical validation failed.

`records_needing_human_review.json` contains records blocked from automatic
import by issues such as duplicate court slugs, invalid populated postcodes,
ambiguous opening hours, or controlled-list mismatches.

`issue_report.json` is a flat issue list with source row numbers and court
slugs, useful for filtering and review.

`import_summary.json` contains the run id, source file, row and status counts,
skipped row count, duplicate slug group count, duplicate slug affected-record
count, mapping warnings, and issue counts by code. Duplicate groups are
conservative for now: every affected record is excluded from `fact_payload.json`
and sent to human review. The importer does not pick a winner or merge duplicate
rows until explicit merge/precedence rules exist.

## Configuration

### Column Mapping

`config/column_mapping.json` maps the Microsoft Forms export columns to logical
field names. It is used during profiling/ingestion to check the workbook shape
and to read values from known columns such as `G` for `court_slug_raw`, address
groups, contact detail groups, and opening-hours groups.

### Field Rules

`config/field_rules.json` describes field-level cleaning, validation, and LLM
normalisation policy in a declarative format. It does not contain executable
Python code. Cleaner names, validator names, and LLM purposes are strings that
later pipeline stages will interpret.

The rules file captures things like:

- whether a logical field is required
- which deterministic cleaners should run
- which validators should run later
- whether GPT-assisted normalisation is allowed for that field
- the strict instructions the LLM must follow if it is used

Current ingestion does not execute `field_rules.json` yet. The next validation
and vocabulary tasks will use these rules to decide which fields need review,
which vocabularies to check, and which ambiguous values are eligible for
LLM-assisted normalisation.

### Vocabularies

`config/vocabularies.example.json` contains local controlled-list examples for
values that must later match FaCT-compatible types, such as address types, court
types, areas of law, contact description types, opening-hours types, food and
drink options, hearing enhancement options, and counter service assistance.

`fact_form_importer.validators.vocabularies` loads these lists and supports:

- exact matching against a code, display name, or alias
- normalised matching for harmless case and whitespace differences
- boolean membership checks for validators

No external API calls happen at this stage. The local vocabulary loader is a
pipeline boundary: later validators can augment or replace the file-backed
values with FaCT API data while keeping the same matching behaviour.

### Validation Status

`fact_form_importer.validators.business_rules` validates ingested
`CourtSubmission` records after deterministic cleaning. It does not call the
FaCT API, Ordnance Survey, or the LLM yet.

Current validation checks:

- required court slug
- optional email and phone syntax
- populated address postcode syntax
- opening-hours time shape and ambiguous time status
- controlled-list values when vocabularies are loaded
- duplicate `court_slug` values across a batch

Status is recalculated after validation:

- `failed`: required court identifier is missing or an error issue exists
- `needs_human_review`: duplicate slug, invalid populated postcode, ambiguous
  opening hours, invalid time, or controlled-list mismatch
- `processed_with_warnings`: optional email/phone warnings or slug
  normalisation from a URL/free text
- `processed`: no validation issues

Address existence checks against Ordnance Survey/FaCT API are intentionally not
part of this step. They should run later as API-backed validation, after syntax
cleaning and before final import, so possible postcode/address matches can be
reviewed rather than treated as a simple regex pass/fail.

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
