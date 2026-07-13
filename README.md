# fact-form-importer

Processes Microsoft Forms XLSX/CSV exports containing Find a Court or Tribunal
court information. The importer will convert spreadsheet rows into structured,
cleaned, validated data for later FaCT API import.

The project is intentionally incremental. The current pipeline performs
deterministic cleaning, FaCT API-backed validation, review output generation,
and read-only approval user export. Optional LLM normalisation is available for
strictly selected public-text and unresolved vocabulary fields, but requires an
explicit CLI flag and environment circuit breaker.

## Goals

- Read Microsoft Forms XLSX/CSV court submission exports.
- Create one `CourtSubmission` object per spreadsheet row.
- Preserve raw values, cleaned values, and validation issues.
- Use deterministic Python cleaners before any LLM-assisted normalisation.
- Use the configured OpenAI-compatible GPT-5.5 deployment only for configured
  ambiguous fields.
- Keep Python responsible for the final FaCT payload shape.
- Generate import JSON, NSU review workbook, summary logs, issue reports, and
  read-only approval user outputs.

## Commands

These are the maintained commands for the current importer workflow.

```bash
sh scripts/bootstrap.sh
```

Creates the local virtual environment, installs the package and dev
dependencies, and installs the pre-push hook. Run once per checkout.

```bash
source .venv/bin/activate
```

Activates the local environment so commands use the project dependencies.

```bash
python3 -m pytest tests/unit --cov=fact_form_importer --cov-report=term-missing --cov-report=json:coverage.json
python3 scripts/check_coverage.py coverage.json --fail-under 90 --core-fail-under 95
```

Runs the unit suite with the same exact coverage thresholds used before
pushing. The second command checks the unrounded coverage JSON values: global
coverage must be at least 90%, and core groups must be at least 95%. Core
groups include deterministic cleaners, ingestion, validation, API-readiness
generation, archive publishing, and the local review UI.

```bash
python3 -m fact_form_importer profile --input "./input/microsoft-forms-export.xlsx" --output "./out"
```

Profiles the Microsoft Forms workbook without cleaning or transforming it. Use
this to confirm the source file shape, row count, headers, and empty columns.

```bash
python3 -m fact_form_importer ingest --input "./input/microsoft-forms-export.xlsx" --output "./out"
```

Runs deterministic ingestion only and writes intermediate raw/cleaned
submission files. Use this when checking column mapping and cleaner behaviour
before full validation.

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out"
```

Runs the current end-to-end non-LLM import preparation: profiling, ingestion,
FaCT API-backed validation, issue/status calculation, controller-ready import
payload JSON, NSU review workbook, and read-only approval user outputs.

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out" --use-llm
```

Runs the same pipeline with optional LLM normalisation. This requires
`LLM_ENABLED=true` and configured OpenAI settings. It makes at most one
row-level model call for each record with safe selected fields, plus one retry
only if the structured response cannot be parsed.

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out" --allow-local-vocabularies
```

Runs the same pipeline with local vocabulary fixtures when FaCT API access is
unavailable. Use only for offline/local inspection; normal runs should use
`vocabulary_source: fact_data_api`.

```bash
python3 -m fact_form_importer serve --output "./out"
```

Starts the local review UI at `http://127.0.0.1:5000`. It lists completed
archived runs, shows raw and cleaned records, issues and API readiness details,
and can upload one XLSX/CSV for background processing. It only binds to
localhost because the review views contain contact and submitter data.

```bash
python3 -m fact_form_importer api-check-court --output "./out" --run-id "<run-id>" --court-slug "<court-slug>"
```

Re-resolves one existing court by slug and checks each target FaCT section
without making a write. This records `ready`, `blocked`, or `unknown` action
states in `out/execution-state/<run-id>.json`; it is the required first step
before executing any action.

```bash
python3 -m fact_form_importer api-execute-action --output "./out" --run-id "<run-id>" --court-slug "<court-slug>" --action-id "<action-id>" --confirm
python3 -m fact_form_importer api-execute-court --output "./out" --run-id "<run-id>" --court-slug "<court-slug>" --confirm
```

Executes one reviewed API action, or all currently safe actions for one court.
Both require `FACT_DATA_API_WRITES_ENABLED=true` in `.env` as well as the
explicit `--confirm` acknowledgement. There is deliberately no all-courts
write command.

```bash
python3 -m fact_form_importer check-llm
```

Checks the configured OpenAI-compatible endpoint, API key, and model with a
tiny prompt. It does not read spreadsheet data or write import outputs.

```bash
python3 -m fact_form_importer llm-test
```

Sends a fake structured normalisation request to the configured model and
prints the JSON response. It exercises the client without using real workbook
data.

```bash
python3 -m fact_form_importer llm-request-review --input "./input/microsoft-forms-export.xlsx" --output "./out"
```

Writes `out/llm_request_review.json` without calling the model. Use it to
inspect exactly which safe field payloads, relevant vocabularies, field rules,
system instructions, and response schema would be supplied by `run --use-llm`.
It works while `LLM_ENABLED=false` and does not include actual court slugs,
metadata, unselected fields, credentials, or endpoint details.

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
python3 -m pytest tests/unit --cov=fact_form_importer --cov-report=term-missing --cov-report=json:coverage.json
python3 scripts/check_coverage.py coverage.json --fail-under 90 --core-fail-under 95
```

Coverage is checked against exact unrounded percentages. The global threshold
is 90%; core coverage is checked at 95% for cleaners, ingestion, validators,
API-readiness/archive processing, and the local review UI.

### 2. Configure local environment

Copy the environment template and populate local values. `.env` is for local
secrets and machine-specific values, and is ignored by git. Keep `.env.example`
as a blank template.

```bash
cp .env.example .env
```

LLM-assisted processing is disabled by default. Leave `LLM_ENABLED=false` for
the deterministic/API-backed pipeline. The manual LLM commands can still test
credentials and structured responses. To permit the optional run stage, set:

```text
LLM_ENABLED=true
OPENAI_BASE_URL=https://<your-ai-foundry-resource>.services.ai.azure.com/openai/v1
OPENAI_API_KEY=<your-api-key>
OPENAI_MODEL=<your-deployment-name>
```

The OpenAI client will use the newer `from openai import OpenAI` style with
`base_url`, `api_key`, and `model`; no separate Azure OpenAI API version is
configured.

#### LLM field selection

LLM use is deliberately split into selection and application. The normal
`run` command never calls the model. `run --use-llm` calls it only when both
the CLI flag and `LLM_ENABLED=true` are present.

Selection is handled by `fact_form_importer.llm.normalise.select_llm_fields`.
It uses `config/field_rules.json` and the loaded vocabularies to build a small
allow-list of candidate fields. It never sends the full spreadsheet row,
metadata, slugs, postcodes, phone numbers, email addresses, yes/no values, or
ordinary opening-hours time fields.

For controlled vocabulary fields, Python gets first pass. If the value already
matches the configured vocabulary exactly or after normalisation, it is not sent
to the LLM. Only unresolved public-facing text or ambiguous vocabulary values
are selected. Fields containing embedded email, phone, or postcode data are
also excluded. Each request contains only selected fields, their relevant
allowed vocabulary values, and their field-specific rules. Court slugs and all
metadata remain out of the request.

To sanity-check the configured endpoint, API key, and model without running the
import pipeline:

```bash
python3 -m fact_form_importer check-llm
```

This sends a tiny prompt and prints the endpoint, model, `LLM_ENABLED` state,
and response preview. It does not read the spreadsheet or write import outputs.

To exercise the structured LLM normalisation client with a fake non-production
record:

```bash
python3 -m fact_form_importer llm-test
```

This sends only fake non-production values covering the current LLM-enabled
rule categories: accessible toilet public text, hearing enhancement, food and
drink, address type, areas of law, court type, counter service assistance,
contact description, contact explanation, and opening-hours type. It prints a
sanity-check transcript with the input fields sent to the model, the output
fields returned by the model, any issues, and the final result summary. It is a
manual test command and does not use real workbook data.

`llm-test` deliberately calls the configured model even when `LLM_ENABLED=false`
because it is a connection and response-shape sanity check. The `LLM_ENABLED`
setting is a circuit breaker for the real pipeline: `run --use-llm` fails fast
unless it is enabled.

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
Time cleaning also repairs unambiguous punctuation and redundant zero-minute
entries, such as `08:.30`, `15::00`, `10.00 AM` with `00:00`, and `14:00PM`
with `00:00`. Exact text statuses such as `Appointment only` and `Counter
service not available` are classified as statuses rather than invalid clock
values, but still require review because FaCT opening-time entries require real
opening and closing times. The original spreadsheet text remains available in
`submissions_raw.json`.

`ingest_summary.json` contains counts for ingested submissions, skipped empty
rows, failed rows, warning rows, and mapping warnings. Use it as the first check
that ingestion behaved as expected.

The full `run` command uses these `CourtSubmission` records for validation,
vocabulary normalisation, NSU review workbook generation, read-only approval
user export, and `fact_import_payload.json` creation. With `--use-llm`, selected
fields are normalised, safely merged, and then validated again before outputs
are written.

### 6. Run the full importer

The default `run` command profiles the workbook, ingests rows, validates
submissions, calculates statuses, and writes draft import/review JSON outputs.
With `--use-llm`, it additionally selects safe fields, makes one row-level LLM
request only where needed, safely applies acceptable results, and validates the
batch again before writing outputs.

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out"
```

To apply the optional LLM stage, first set `LLM_ENABLED=true`, then run:

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out" --use-llm
```

The flag is intentionally explicit: setting `LLM_ENABLED=true` alone does not
send records to the model.

The `run` command requires `FACT_DATA_API_BASE_URL` and
`FACT_DATA_API_BEARER_TOKEN` so controlled lists come from the FaCT Data API.
If either value is missing, or the API rejects the token, the command fails
instead of silently validating against stale local data.

The review UI reads `.env` when its server process starts. After refreshing
`FACT_DATA_API_BEARER_TOKEN`, stop and start `serve` again before uploading or
executing actions. It uses the fixed local address `http://127.0.0.1:5000`.

Keep `FACT_DATA_API_WRITES_ENABLED=false` in normal use. It is a separate
circuit breaker for the post-review, per-court API execution commands and UI
controls. Reading vocabularies, validating slugs, generating an action plan,
and preflighting a court do not need it. A write requires both
`FACT_DATA_API_WRITES_ENABLED=true` and explicit confirmation in the CLI or UI.

For local/offline inspection only, you can bypass the API and use the checked-in
example vocabularies:

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out" --allow-local-vocabularies
```

Every successful `run` first writes to a temporary staging directory, then
publishes an immutable archive at `out/final/<run_id>/`. The existing flat
files in `out/` are refreshed as a latest-run convenience view, and
`out/latest_run.json` identifies the immutable archive. Uploaded UI source
files are deleted after processing; the derived raw JSON remains archived.

Each archive contains:

```text
out/fact_import_payload.json
out/failed_records.json
out/records_needing_human_review.json
out/issue_report.json
out/import_summary.json
out/api_readiness_report.json
out/run_manifest.json
out/llm_request_review.json
out/nsu_cleaned_review.xlsx
out/read_only_approval_users.json
out/read_only_approval_users.xlsx
```

`submissions_cleaned.json` contains every final validated submission, including
its final status and issues. It is the all-record source used by the local
review UI; `submissions_raw.json` remains the unmodified row extraction.

`fact_import_payload.json` is the single, versioned JSON request body for the
future FaCT import controller. It uses camelCase and contains only records with
status `processed` or `processed_with_warnings`, provided they have no blocking
error issues. In a normal FaCT API-backed run, each record has its resolved FaCT
court UUID and slug, source row number for traceability, and complete API-aligned sections for facilities,
accessibility, translation, professional information, counter-service opening
hours, addresses, contacts, and court opening hours. Controlled list values are
resolved to FaCT UUIDs where the API provides them. It deliberately excludes raw
spreadsheet values, submitter metadata, issue objects, and records needing
review. This importer does not send the payload to FaCT; a future controller
will accept this document as one JSON body.

An offline `--allow-local-vocabularies` run can still generate the same shape
for inspection, but its `courtId` values are `null` because it deliberately
does not call FaCT. Do not use an offline-fallback payload as controller input.

`api_readiness_report.json` is the immutable, endpoint-shaped action plan for
the reviewed run. It is not a new FaCT controller payload and it does not
create courts. Actions are derived only for importable records and contain the
existing endpoint, method, target path, request body and source-field evidence.
Before each write, the execution layer re-resolves the court by slug and checks
the target API section. Under the current conservative policy, a populated
target section blocks that action for human review rather than replacing,
merging, or deleting existing FaCT data.

The action report is generated against the FaCT API contract in use at the
time of the run. Before a write, the execution layer validates the body again
with the freshly resolved court UUID. This protects older archives from being
sent after the contract changes: incomplete actions are shown as `blocked` with
the missing API fields instead of being retried and receiving a 400 response.
After updating the importer, create a new `run` before executing actions; do
not use an older readiness report as a substitute for regeneration.

Execution state is written separately to
`out/execution-state/<run-id>.json`, never back into the archived action plan.
Action status is one of `planned`, `ready`, `blocked`, `running`, `succeeded`,
`failed`, or `unknown`. A court is `completed` only once every planned action
has succeeded. A timeout is `unknown` and is never automatically retried.

`run_manifest.json` records the source display name, completion time, run
summary, and SHA-256 hashes of every archived artifact. Use it to identify a
historic run and verify its files.

`out/execution-state/` is a local, git-ignored execution ledger rather than an
archive artifact. It lets the UI show whether individual actions have been
checked, blocked, completed, failed, or have an unknown outcome without
changing the generated run evidence or its integrity hashes.

`failed_records.json` contains records that cannot progress because required
data is missing or schema-critical validation failed.

`records_needing_human_review.json` contains records blocked from automatic
import by issues such as duplicate court slugs, invalid populated postcodes,
ambiguous opening hours, or controlled-list mismatches.

`issue_report.json` is a flat issue list with source row numbers and court
slugs, useful for filtering and review.

`llm_request_review.json` is an optional, local review artifact produced by
`llm-request-review`. It records the exact safe structured request bodies the
LLM pipeline would use, alongside the static system instructions and response
schema. It always reports zero model calls; it is an inspection command, not a
normalisation command. Like all files in `out/`, it is git-ignored.

`import_summary.json` contains the run id, source file, row and status counts,
skipped row count, duplicate slug group count, duplicate slug affected-record
count, mapping warnings, issue counts by code, `vocabulary_source`, and whether
LLM processing was enabled. It also records LLM requested state, calls,
failures, parse retries, selected and processed field counts, affected
submissions, model name, and API-readiness ready/pending action counts. In a
normal run, `vocabulary_source` should be
`fact_data_api`. The CLI prints duplicate and LLM metrics at the end of each
run. Duplicate groups are conservative for now: every affected record is
excluded from `fact_import_payload.json` and included in the `needs_human_review`
count. The duplicate count is not an additional status category: a duplicate
record can also have other review issues, such as invalid opening hours. The
importer does not pick a winner or merge duplicate rows until explicit
merge/precedence rules exist.

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

### 7. Review archived runs locally

Start the local UI after at least one completed run:

```bash
python3 -m fact_form_importer serve --output "./out"
```

The landing page lists every valid `out/final/<run_id>/run_manifest.json` from
newest to oldest. Select a run to inspect its summary, records, raw submitted
values, cleaned values, issues, duplicate status and API readiness report. A
record with an action plan has a FaCT API execution table showing the request
body, relevant cleaned values, mapped raw source values, preflight outcome and
current execution state. The UI can check a court, run one action, or run all
safe actions for that one court. Write buttons appear only when
`FACT_DATA_API_WRITES_ENABLED=true`; server-side checks enforce the same rule.
There is no global bulk-write control. Tables support status/court-row filtering
and pagination; every listed archive artifact can be downloaded. The upload
form accepts CSV/XLSX only, runs one job at a time, deletes the original upload
on completion, and offers LLM use only as an explicit checkbox when
`LLM_ENABLED=true`.

This is an unauthenticated local tool. Do not bind it beyond localhost or share
its archive directory without adding an authentication and access-control
design.

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
pipeline stages interpret as the importer grows.

The rules file captures things like:

- whether a logical field is required
- which deterministic cleaners should run
- which validators should run later
- whether GPT-assisted normalisation is allowed for that field
- the strict instructions the LLM must follow if it is used

The importer does not treat this as a generic executable rules engine. It does
use the file as the source of truth for LLM field selection: only fields with
`llm.enabled=true` can be selected by `run --use-llm`.

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
`CourtSubmission` records after deterministic cleaning. During the `run`
command, FaCT API functions are supplied to validation so court slugs,
controlled vocabularies, and high-confidence court-slug suggestions can be
checked against the source of truth. It does not call Ordnance Survey or the
LLM.

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
  populated postcode, ambiguous opening hours, invalid time, controlled-list
  mismatch, LLM failure, unsafe model output, or medium/low LLM confidence
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
