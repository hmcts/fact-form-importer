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

The LLM-assisted steps are disabled by default. Leave `LLM_ENABLED=false` while
running the deterministic/API-backed pipeline. When LLM support is implemented
and you want to use it, set:

```text
LLM_ENABLED=true
OPENAI_BASE_URL=https://<your-ai-foundry-resource>.services.ai.azure.com/openai/v1
OPENAI_API_KEY=<your-api-key>
OPENAI_MODEL=<your-deployment-name>
```

The OpenAI client will use the newer `from openai import OpenAI` style with
`base_url`, `api_key`, and `model`; no separate Azure OpenAI API version is
configured.

To sanity-check the configured endpoint, API key, and model without running the
import pipeline:

```bash
python3 -m fact_form_importer check-llm
```

This sends a tiny prompt and prints the endpoint, model, `LLM_ENABLED` state,
and response preview. It does not read the spreadsheet or write import outputs.

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
API payload. Counter service opening times with both open and close set to
`00:00` are stripped during ingestion because product guidance says this means
the counter has different times that should be added manually in the admin
portal. This rule is scoped to counter service times and does not change general
court opening-hours records. Phone and email cleaners also extract the first
valid UK phone number or email address from free text, and contact-detail phone
and email pairs move misplaced values into the paired empty field where safe.
The original spreadsheet text remains available in `submissions_raw.json`.

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

The `run` command requires `FACT_DATA_API_BASE_URL` and
`FACT_DATA_API_BEARER_TOKEN` so controlled lists come from the FaCT Data API.
If either value is missing, or the API rejects the token, the command fails
instead of silently validating against stale local data.

For local/offline inspection only, you can bypass the API and use the checked-in
example vocabularies:

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out" --allow-local-vocabularies
```

This also writes `profile.json` and the ingest intermediate files listed above.
It then writes:

```text
out/fact_payload.json
out/failed_records.json
out/records_needing_human_review.json
out/issue_report.json
out/import_summary.json
out/nsu_cleaned_review.xlsx
out/read_only_approval_users.json
out/read_only_approval_users.xlsx
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
count, mapping warnings, issue counts by code, `vocabulary_source`, and whether
LLM processing was enabled. In a normal run, `vocabulary_source` should be
`fact_data_api`. The CLI also prints the duplicate group, affected-record, and
LLM-enabled counts at the end of each run. Duplicate groups are
conservative for now: every affected record is excluded from `fact_payload.json`
and sent to human review. The importer does not pick a winner or merge duplicate
rows until explicit merge/precedence rules exist.

`nsu_cleaned_review.xlsx` is a reviewer-friendly Excel workbook. It is not the
machine-readable source of truth; the JSON files remain that. The workbook helps
NSU/product reviewers inspect what deterministic cleaning and validation did
without recreating the original 204-column Microsoft Forms export. It contains
tabs for summary counts, processed records, records needing human review, failed
records, duplicate courts, cleaned addresses, cleaned contacts, cleaned opening
hours, flat issues, and submitter users. The record tabs include
`review_reason` and `suggested_next_action` columns. For controlled-list
failures, `review_reason` identifies the specific field and submitted value that
did not match, while the `Issues` tab provides one row per issue with raw and
cleaned values. The `Duplicate courts` tab includes the duplicate source rows,
completion/start/last-modified dates, submitter names and emails, and a
`candidate_most_recent_row` based on the available form timestamps. This is
review evidence for NSU/product decisions; the importer still does not
automatically migrate only the latest duplicate until that rule is confirmed.

`read_only_approval_users.json` and `read_only_approval_users.xlsx` contain the
unique form submitters who should be considered for the read-only approval role.
Submitter emails are trimmed, lowercased, and deduplicated; source row numbers
are retained so NSU can trace each user back to submitted forms. Users listed in
`config/team_exclusions.json` under `exclude_from_read_only_approval_role` are
removed from the role list and written to `excluded_users` with reason
`configured_exclusion`, because those team members should receive a different
role.

`config/team_exclusions.json` is intentionally git-ignored because it can
contain real staff email addresses. Start from the safe committed template:

```bash
cp config/team_exclusions.example.json config/team_exclusions.json
```

Then add local exclusions to `config/team_exclusions.json`; do not commit that
file.

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

The `run` command loads FaCT-owned controlled lists from the FaCT Data API before
validation:

- `/types/v1/areas-of-law`
- `/types/v1/court-types`
- `/types/v1/opening-hours-types`
- `/types/v1/contact-description-types`

Set both `FACT_DATA_API_BASE_URL` and `FACT_DATA_API_BEARER_TOKEN` in `.env`.
If the API lookup fails, including an invalid or expired bearer token, `run`
fails. This is deliberate: API-owned controlled lists should come from the
source of truth, not an old fixture file.

`--allow-local-vocabularies` is the only fallback path. It is intended for unit
tests and offline review only, and it records `vocabulary_source` as
`local_json` or `local_json_fallback_after_fact_data_api_error`. Local-only form
vocabularies, such as food and drink options or hearing enhancement options,
still come from the JSON file because they are not FaCT Data API type lists.

`fact_form_importer.validators.vocabularies` loads vocabulary entries and supports:

- exact matching against a code, display name, or alias
- normalised matching for harmless case and whitespace differences
- boolean membership checks for validators

The local file is therefore a fallback/test fixture, not the preferred source of
truth for FaCT-owned type lists.

### Validation Status

`fact_form_importer.validators.business_rules` validates ingested
`CourtSubmission` records after deterministic cleaning. It does not call the
FaCT API, Ordnance Survey, or the LLM yet.

Current validation checks:

- required court slug
- cleaned court slug exists in FaCT Data API during the `run` command
- missing court slugs are searched against FaCT court-name search to find
  possible suggestions
- optional email and phone syntax
- populated address postcode syntax
- opening-hours time shape and ambiguous time status
- controlled-list values when vocabularies are loaded
- duplicate `court_slug` values across a batch

Status is recalculated after validation:

- `failed`: required court identifier is missing or an error issue exists
- `needs_human_review`: court slug not found in FaCT, duplicate slug, invalid
  populated postcode, ambiguous opening hours, invalid time, or controlled-list
  mismatch
- `processed_with_warnings`: optional email/phone warnings, slug normalisation
  from a URL/free text, or a very high-confidence court slug auto-repair
- `processed`: no validation issues

Address existence checks against Ordnance Survey/FaCT API are intentionally not
part of this step. They should run later as API-backed validation, after syntax
cleaning and before final import, so possible postcode/address matches can be
reviewed rather than treated as a simple regex pass/fail.

### Issue Codes

Issue codes are used in `issue_report.json` and `nsu_cleaned_review.xlsx`.
The workbook keeps the codes for filtering, but also includes plain-English
review reasons and suggested next actions.

Current issue meanings:

- `COURT_SLUG_NORMALISED`: the submitted court identifier was changed into a
  clean slug, for example from a full Find a Court URL to `fleetwood-court`.
  This is usually non-blocking.
- `COURT_SLUG_AUTO_REPAIRED`: the cleaned slug did not exist in FaCT, but
  FaCT court-name search returned a very high-confidence match of at least
  `0.95` and `GET /courts/slug/{suggestedSlug}/v1` verified that suggested slug
  exists. The row can still be imported, but the repair is visible as a warning
  in `issue_report.json` and the NSU workbook.
- `COURT_SLUG_NOT_FOUND`: the cleaned slug is syntactically valid but
  `GET /courts/slug/{courtSlug}/v1` did not find it in FaCT Data API. The row is
  blocked for human review because it may be a typo, an obsolete slug, or a
  court that needs separate product/NSU confirmation.
- `COURT_SLUG_SUGGESTED`: the cleaned slug did not exist in FaCT, and FaCT
  court-name search found a possible match below the auto-repair threshold. The
  suggested slug, court name, confidence and query are written to the NSU
  workbook, but the row remains blocked for human review.
- `DUPLICATE_COURT_SLUG`: more than one submitted row resolves to the same
  court slug. All affected rows are blocked from automatic import until a
  reviewer decides whether to merge, discard, or correct them.
- `INVALID_EMAIL`: an email value could not be parsed as a valid email address.
  Optional invalid emails are preserved for review rather than silently dropped.
- `INVALID_PHONE`: a phone value could not be parsed as a possible UK phone
  number. Optional invalid phones are preserved for review.
- `INVALID_POSTCODE`: a populated address postcode does not match the expected
  UK postcode format. This blocks automatic import because address data would
  be unreliable.
- `INVALID_TIME`: an opening-hours value could not be parsed as a valid `HH:MM`
  time.
- `MISSING_COURT_IDENTIFIER`: the row has business data but no usable court
  slug. This is a failed record until a valid court slug is added.
- `OPENING_HOURS_AMBIGUOUS`: opening hours need review because the time values
  are invalid or ambiguous.
- `POSTCODE_TYPO_REPAIRED`: an obvious `O`/`0` typo in a postcode digit
  position was repaired, for example `CRO 2RF` to `CR0 2RF`. This is
  non-blocking and visible in the issue report for audit. Other invalid
  characters, such as `CF10 £PG`, are not guessed and remain
  `INVALID_POSTCODE` for review.
- `VOCAB_NO_MATCH`: a value does not match the configured controlled list, such
  as court type, area of law, contact description, opening-hours type, food and
  drink option, or hearing enhancement option.

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
