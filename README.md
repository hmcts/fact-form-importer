# fact-form-importer

Processes Microsoft Forms XLSX/CSV exports containing Find a Court or Tribunal
court information. The importer will convert spreadsheet rows into structured,
cleaned, validated data for later FaCT API import.

The project is intentionally incremental. The current pipeline performs
deterministic cleaning, FaCT API-backed validation, optional FaCT/Ordnance
Survey address verification, review output generation, and read-only approval
user export. Optional LLM normalisation is available for strictly selected
public-text and unresolved vocabulary fields, but requires an explicit CLI flag
and environment circuit breaker. Model-derived API values are captured in an
immutable per-run review artifact and gated by a separate mutable approval
ledger before execution.

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
- Preserve exact LLM/OS decision evidence and require a manual or strict
  policy-based field approval before a dependent API action can use it.

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
FaCT API-backed validation, issue/status calculation, API-aligned inspection
JSON, endpoint action plan, NSU review workbook, and read-only approval user
outputs.

When the run uses live FaCT vocabularies, successful archive publication is
followed by the required read-only live-section comparison scan. The API
changes page therefore opens with current/submitted/effective results already
prepared and defaults to `Needs review`; empty, unchanged, and already approved
sections remain available through `All sections` but do not clutter the working
queue. Approving a live-data change advances to the next pending comparison.
Its refresh control is a retry for interrupted scans or a deliberate recheck
after live FaCT data changes. It never writes to FaCT. High-confidence model
values may already be policy-approved, but live-data replacement remains a
separate decision because a complete section diff has no LLM confidence score.
The narrow `explicit-professional-information-v1` policy is an exception: it
automatically approves a fresh, unambiguous professional-information comparison
when every effective difference is an explicit submitted interview-room value.
Inferred room-count defaults and all other live-data changes remain manual.
Reviewers may record every currently approvable Step 2 decision in one atomic
operation with `Approve all approvable changes`. This applies across every page,
excludes unchecked, empty, unchanged, already approved, and ambiguous matches,
and performs no FaCT API writes.

Singleton sections are merged recursively, including nested objects such as
`professionalInformation`. Submitted leaf values replace their live
counterparts while unsubmitted nested values are preserved; supplying one
professional-information field never replaces the whole nested block. Explicit
clear paths remain the only way to remove a preserved nested value.

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out" --verify-addresses
```

Adds optional address verification through FaCT's existing authenticated
`/search/address/v1/postcode/{postcode}` endpoint. The importer never calls
Ordnance Survey directly or reads an OS key. It makes at most one uncached
postcode lookup every 1.1 seconds, changes an address only for a unique,
very-high-confidence match, and records every result in
`address_verification_report.json` and the review workbook.

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out" --use-llm
```

Runs the same pipeline with optional LLM normalisation. This requires
`LLM_ENABLED=true` and configured OpenAI settings. It makes at most one
row-level model call for each record with safe selected fields, plus one retry
only if the structured response cannot be parsed. Independent rows run in a
bounded pool (eight by default), but their request contents and returned values
are never combined.

`--verify-addresses --use-llm` may additionally send unresolved OS candidate
comparisons to the same row-level request. It sends no postcode, contact data,
or court slug. The model can only select a supplied UPRN as an advisory review
suggestion; it never changes archived address evidence itself. A plausible
first-line discrepancy is treated as weaker than consistent building, street,
and town evidence. Strict automatic approval is limited to an actionable,
high-confidence selection of the sole supplied OS candidate; all other usable
selections remain pending for a reviewer.

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
and can upload one XLSX/CSV for background processing. From a run summary, use
`LLM actions review` to inspect exact field/address evidence and approve pending
results; `LLM review factors` is read-only. Approval never executes an API
action—use the record, court, or run execution control separately. Review items
are ordered from high to low confidence and can be filtered by confidence.
Approval has no confirmation dialog and moves to the next pending result while
preserving filters and pagination. Where an action cannot be planned, the page
explains the blocker in plain English and keeps internal issue codes inside
expandable technical details. The UI only binds to localhost because the review
views contain contact and submitter data.

```bash
python3 -m fact_form_importer api-check-court --output "./out" --run-id "<run-id>" --court-slug "<court-slug>"
```

Re-resolves one existing court by slug and checks each target FaCT section
without making a write. This records `ready`, `blocked`, or `unknown` action
states in `out/execution-state/<run-id>.json`. It is useful for reviewing one
court before writing; every write command repeats the same live checks.

If a preflight fails, the record view and ledger now retain a safe diagnostic,
including the HTTP status where the API supplied one. `HTTP 401` or `HTTP 403`
means the FaCT bearer token needs refreshing and the review UI must be restarted.
A connection error means the FaCT **application** is not reachable at
`FACT_DATA_API_BASE_URL`; a database container by itself is not enough.

```bash
python3 -m fact_form_importer api-execute-action --output "./out" --run-id "<run-id>" --court-slug "<court-slug>" --action-id "<action-id>" --confirm
python3 -m fact_form_importer api-execute-court --output "./out" --run-id "<run-id>" --court-slug "<court-slug>" --confirm
python3 -m fact_form_importer api-execute-run --output "./out" --run-id "<run-id>" --confirm
```

Executes one currently safe API action, or all currently safe actions for one
court. `api-execute-run` processes importable courts sequentially in slug order.
It re-resolves each court and preflights every action immediately before a
write, continues after failures, and does not automatically retry an earlier
`blocked`, `failed`, or `unknown` action. All three commands require
`FACT_DATA_API_WRITES_ENABLED=true`, an existing FaCT user UUID in
`FACT_DATA_API_USER_ID`, and explicit `--confirm` acknowledgement. FaCT uses
that UUID to attribute its audit records; the importer never creates a user
automatically. The batch command prints counts and grouped attention themes;
use `out/execution_summary.json` or the execution-summary page in the local UI
for the complete per-court action list. The execution summary also groups
attention outcomes by API request type. Each request-type group shows the
method/endpoint template, affected action and court counts, distinct reasons,
whether the outcome was a preflight block or an API rejection, example courts,
and decision guidance. Postcode-specific FaCT/OS no-result messages are grouped
as one distinct reason while retaining an example of the original diagnostic.

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

Add `--verify-addresses` to inspect the equally minimal unresolved address
candidate payloads. This runs the rate-limited FaCT postcode lookups but still
makes zero model calls.

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
LLM_MAX_CONCURRENCY=8
OPENAI_BASE_URL=https://<your-ai-foundry-resource>.services.ai.azure.com/openai/v1
OPENAI_API_KEY=<your-api-key>
OPENAI_MODEL=<your-deployment-name>
```

The OpenAI client will use the newer `from openai import OpenAI` style with
`base_url`, `api_key`, and `model`; no separate Azure OpenAI API version is
configured. `LLM_MAX_CONCURRENCY` is capped between 1 and 16; reduce it if the
Azure deployment reports capacity or rate-limit errors.

Address verification uses the existing FaCT API credential settings, not a
direct Ordnance Survey integration:

```text
FACT_DATA_API_BASE_URL=http://127.0.0.1:8989
FACT_DATA_API_BEARER_TOKEN=<fact-api-token>
OS_ADDRESS_MIN_INTERVAL_SECONDS=1.25
```

Ordnance Survey currently documents a limit of 50 transactions per minute for
development-mode projects and partner trials, and 600 transactions per minute
for live-mode projects. Leave `OS_ADDRESS_MIN_INTERVAL_SECONDS` unset or at
`1.25` for development/trial use. A confirmed live-mode migration project can
use `0.11`, which is approximately 545 requests per minute and leaves headroom
below the published live limit. Values below `0.10` are clamped to `0.10`.
The limiter caches duplicate postcodes and retries once after an HTTP 429.
`OS_KEY` is present in `.env.example` only as a reminder for the separately
running FaCT Data API; this importer does not read it. See the
[OS rate-limiting policy](https://docs.os.uk/os-apis/core-concepts/rate-limiting-policy).

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

LLM status is deliberately field-scoped. A selected field returned with medium
or low confidence, or explicitly marked for review, holds that submission for
review. The model's aggregate response confidence and aggregate review flag are
retained as audit notes only: they cannot downgrade a whole court when the
uncertainty relates to a separate field or an advisory OS address candidate.
Unresolved OS address verification blocks only the affected address action in
the API-readiness plan; it does not itself downgrade the court submission.

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
with `00:00`. It can recover one complete `HH:MM` value followed by recognised
noise such as `?` or a duration suffix and records `TIME_FORMAT_RECOVERED` with
the original and repaired values. It does not infer ranges or repair ambiguous
values such as `09-4.30:09-4.30`. An explicit `No counter service available`
status remains truthful in cleaned evidence without invented hours; a
contradictory assistance selection remains for review. FaCT requires every
effective opening-hours request to contain a period, so an empty effective
counter-service request is blocked locally before any write. The original
spreadsheet text remains available in `submissions_raw.json`.

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

To add FaCT-backed address verification, use the explicit flag:

```bash
python3 -m fact_form_importer run --input "./input/microsoft-forms-export.xlsx" --output "./out" --verify-addresses
```

The verification stage runs after deterministic cleaning and initial validation.
It calls FaCT's existing postcode search once for each unique supported
postcode, at the configured minimum interval. A single, clearly matching OS
candidate can update the structured address; ambiguous/no-result cases remain
unchanged with evidence for review. An address issue blocks only that address
write action, not unrelated safe actions for the same court.

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
out/address_verification_report.json
out/submission_selection.json
out/run_manifest.json
out/llm_request_review.json
out/nsu_cleaned_review.xlsx
out/duplicate_forms_review.xlsx
out/read_only_approval_users.json
out/read_only_approval_users.xlsx
```

`submissions_cleaned.json` contains every submitted row. The latest completed
row for each cleaned court slug is authoritative; older duplicates are retained
with `selection_status=superseded`, a link to the selected row, and operational
status `skipped`. `submission_selection.json` is the immutable, versioned
evidence for that decision. Only authoritative rows proceed through validation,
OS/LLM work, approvals, API planning and execution.
Court identity is first canonicalised through FaCT so aliases such as a singular
court name or dotted slug cannot evade duplicate grouping. Preliminary issues
from that identity pass are discarded; operational validation still runs only
for the selected latest rows.

`fact_import_payload.json` is a versioned, API-aligned JSON snapshot for
inspection and possible future bulk-import work. It is not used by the current
safe execution workflow, which instead uses the endpoint action plan described
below. It uses camelCase and contains only records with
status `processed` or `processed_with_warnings`, provided they have no blocking
error issues. In a normal FaCT API-backed run, each record has its resolved FaCT
court UUID and slug, source row number for traceability, and complete API-aligned sections for facilities,
accessibility, translation, professional information, counter-service opening
hours, addresses, contacts, and court opening hours. Controlled list values are
resolved to FaCT UUIDs where the API provides them. It deliberately excludes raw
spreadsheet values, submitter metadata, issue objects, and records needing
review. This importer does not send the payload to FaCT.

An offline `--allow-local-vocabularies` run can still generate the same shape
for inspection, but its `courtId` values are `null` because it deliberately
does not call FaCT. Do not use an offline-fallback payload as controller input.

`api_readiness_report.json` is the immutable, endpoint-shaped action plan for
the reviewed run. It is not a new FaCT controller payload and it does not
create courts. Manifest version 1.9 plans complete, valid sections independently,
so an unrelated source issue does not hide a safe proposal. Address, contact,
and opening-hours entries are grouped into one logical section action. Duplicate
court rows use the latest completion time, then last-modified/start time and
finally the highest source row. Before each write, execution compares the
submitted proposal with the live target. Submitted values overwrite matching
live values; blanks preserve live data unless an approved explicit clear exists.
Collection entries match by business type. Unmatched live entries are
preserved, new types are added, and ambiguous duplicate types are held.

For professional information, the form supplies only interview-room values.
The request-only fallbacks for `videoHearings`, `commonPlatform`, and
`accessScheme` are `false` when an empty FaCT section must be completed, but
they are not treated as submitted answers. A populated live value therefore
wins over those fallbacks. The action records the fallback fields and a visible
`migration_assumptions` item without changing the source submission. A fresh,
unambiguous comparison is automatically approved only when its remaining
effective differences are explicit `interviewRooms`, `interviewRoomCount`, or
`interviewPhoneNumber` values. A fallback room count such as `1` remains
manually reviewable.

### API-required values not collected by the form

The importer does not change a submitted accessibility Yes/No answer merely to
satisfy the API. A supplied accessible-entrance support number is used. When
the unchanged FaCT API requires an accessible-entrance or lift-support number,
the form and live FaCT section are checked first. A real live number is
preserved when the form is blank. If neither source has a number, the reviewed
effective request uses the product-approved request-only placeholder
`00000000000`. This never changes raw or cleaned evidence and is displayed in
the action's migration assumptions and live comparison before approval.

There is one narrow, reviewed numeric-default policy for dependent fields where
the controlling Yes/No answer is present and FaCT requires a numeric value:

- `lift=true` with a blank door width sends `liftDoorWidth=1` centimetre.
- `lift=true` with a blank weight limit sends `liftDoorLimit=1` kilogram.
- `interviewRooms=true` with a blank room count sends `interviewRoomCount=1`.
- `interviewRooms=false` sends `interviewRoomCount=0`, including when the
  optional form count contradicts that No answer.

Explicit, unambiguous lift measurements are preserved through request-only unit
normalisation. Door widths supplied in millimetres, centimetres, or metres are
sent as integer centimetres, and limits carrying `kg`/`kgs` are sent as integer
kilograms (for example, `800 mm` becomes `80` cm and `650KG` becomes `650` kg).
Ambiguous prose, conflicting measurements, zero, and values that cannot be
represented exactly as the required positive integer remain pending.
Recognised whole-value unavailable answers such as `n/k`, `u/k`, `unknown`,
`not known`, `not sure`, and `N/A` follow the blank-value minimum policy. Other
unparseable descriptive text, conflicting measurements, and explicit zero do
not.

These defaults are request-only: raw and cleaned source data are unchanged, and
each action displays a `Migration assumptions` entry in the UI/action plan.
They are never used for an explicit zero, an unrecognised or ambiguous value,
or a missing parent Yes/No answer; those actions remain pending. The data is
then reviewed through the existing approval feature in `fact-admin-frontend`.
Execution derives the same lift minimums for older archived actions created
before this policy, but only when immutable cleaned source evidence confirms
that `lift=true` and the dependent answer was blank or a recognised unavailable
marker. Explicit invalid text remains blocked. A refreshed FaCT comparison displays the resulting
effective request without modifying the archived action or submission.

The action report is generated against the FaCT API contract in use at the
time of the run. Before a write, the execution layer validates the body again
with the freshly resolved court UUID. This protects older archives from being
sent after the contract changes: incomplete actions are shown as `blocked` with
the missing API fields instead of being retried and receiving a 400 response.
For address actions, a `--verify-addresses` run records immutable FaCT/OS
evidence. A verified or safely normalised address reuses that evidence during
execution; an older or unverified report does a fresh, shared rate-limited
postcode lookup before the write. A 400/404 postcode response blocks only that
address action with the API's reason. A 429, timeout, or service outage is
`unknown` and can be checked again later. Request-body text repairs are limited
to conventional equivalents that FaCT accepts: `C/o` becomes `care of`, `&`
becomes `and`, a vertical-bar separator becomes ` - `, and standalone `N/A`
becomes `Not applicable`. Every change is recorded in
`request_body_normalisations`; archived raw and cleaned evidence is unchanged.
Scottish postcodes are supported following the FaCT Data API contract change in
PR #313. The importer only pre-classifies `BT`, `IM`, `JE`, and `GY` postcode
regions as unsupported; all valid Scottish postcodes proceed to the FaCT/OS
lookup and normal address validation.
Invalid optional contact emails are omitted from the request while the raw
value and warning remain auditable. If no useful channel or explanation remains,
that empty proposed contact item is omitted so live merge data is preserved.
Counter-service metadata may be planned without submitted hours because existing
live periods can complete the merged request. The final effective request must
contain at least one period; otherwise execution blocks locally without calling
FaCT. Unusable submitted periods are omitted and existing live periods survive
the merge. The `No counter service available` pseudo opening-hours type is not a
publishable court-opening period and is omitted from that collection. A new
`run` gives the clearest immutable plan, while the current-run overlay reconciles
these changes for unsucceeded actions only.

When a reviewer explicitly omits every stable submitted item in an address,
contact or court-opening-hours collection, merge semantics preserve the entire
live FaCT collection. The section is therefore completed as a no-op: it does not
require value or live-data approval, a court lookup, or an API write. The review
pages label it `No execution required` and do not offer an individual run
control. An empty collection without explicit item-by-item omission evidence
remains blocked; a blank form section never means “delete everything.”

Execution state is written separately to
`out/execution-state/<run-id>.json`, never back into the archived action plan.
It records local preflight and write outcomes only; it is not a replacement for
the existing review and approval workflow in `fact-admin-frontend`. Every write
still repeats the live target comparison. Existing-data change approval is
bound to canonical hashes of the live before state and effective merged after
state; a changed target or proposal returns the action to review. Singletons
are overlaid field by field. Collections update matching typed entries and
create new types while preserving every unmatched live entry. No surplus
DELETE operations are generated. A failed or unknown multi-operation update
re-reads FaCT and records the partial state for attention.
Professional-information approvals record whether they were manual or applied
by `explicit-professional-information-v1`; policy approval never starts a FaCT
write.
Action status is one of `planned`, `awaiting_approval`, `ready`, `blocked`,
`running`, `succeeded`, `failed`, or `unknown`. A court is `completed` only
once every planned action has succeeded. `blocked` means no write was attempted for that action because
the plan or live FaCT preflight identified a review requirement; it is not a
failed API request. A timeout is `unknown` and is never automatically retried.
Confirmed `succeeded` and `blocked` states are retained if a later live
preflight cannot reach FaCT, so a transient token or connectivity problem does
not erase prior execution evidence.

Runs that use the LLM also archive `llm_actions_review.json`. It records each
field result and unresolved-address comparison, including the exact safe LLM
input, OS candidates, selected UPRN, proposed address mapping, and dependent API
actions. Medium/low-confidence, unresolved, or review-flagged model results
must be approved individually in the local review UI. A strict versioned policy
automatically approves an address when the model selected any supplied OS
candidate with high confidence, did not request review, and the result has an
actionable address dependency. The prompt treats a plausible first-line
discrepancy as a weaker signal, but still rejects conflicting
town/street/building evidence, competing plausible institutional candidates,
and matches requiring invented information.
Approvals are idempotent, do not execute an API request, and are stored outside
the immutable archive in `out/llm-approval-state/<run-id>.json`, with manual or
policy provenance. An action with one or more unapproved LLM dependencies
remains `awaiting_approval`. Approved OS mappings are applied only to the
eventual request body, and execution confirms that the selected UPRN is still
returned before writing.

Policy `high-accepted-field-v2` approves every accepted high-confidence field
`set` or allowlisted `clear` result when the model did not request human review,
including changed wording and format-only changes. Earlier
`high-unchanged-field-v1` decisions remain valid and distinguishable in the
ledger. Unresolved, review-flagged, and medium/low-confidence results remain
reviewable. Reviewers may
edit the five address text components shown on the LLM actions page before
execution. The ledger stores the canonical approved patch and its hash while
the archived OS/LLM evidence remains unchanged. Editing an automatic approval
converts it to a manual decision, retains decision history, resets safe
unexecuted action state, and invalidates stale FaCT comparisons. Address type,
areas of law, court types, and selected UPRN remain read-only; execution still
requires the selected UPRN at the approved postcode.

When the model returned no selection but the immutable run contains supplied OS
candidates with UPRNs, the address review page provides a manual candidate
selector. The reviewer chooses one supplied candidate, may edit the five mapped
address text fields, and must record a rationale. Approval-ledger schema 1.6
stores the chosen UPRN, rationale, canonical patch/hash, timestamp and decision
history without changing the immutable OS/LLM artifact. These decisions are
excluded from policy and bulk approval, are unavailable after execution starts,
succeeds or becomes uncertain, invalidate stale target approvals when changed,
and still require the selected UPRN in a fresh postcode lookup before writing.

The structured field response has an explicit `set`, `clear`, or `unresolved`
operation. `clear` is initially allowed only for optional
`contacts[*].explanation`: opening days and times are removed because structured
opening-hours fields carry them, and the supplied National Contact Centre-only
example clears to “Not supplied”. Accessible-toilet floor text uses UCD wording:
`Ground floor` becomes `Available on the ground floor.`, and multiple floors use
`Available on the ground, first and third floors.` without inventing locations.
PO Box addresses are allowed and have no separate manual-only dependency. They
follow the same OS lookup, LLM confidence, approval-policy, and execution rules
as every other address.

Verification for this workflow: 433 unit tests pass, Ruff is clean, exact global
coverage is 90.19%, and every configured 95% core coverage gate passes
(API manifest 95.47% and web 95.08%).

The latest full OS/LLM run is `20260722T112455Z-b4cce71d`, generated from
`input/input-csv-4.xlsx`: 472 source submissions, 416 authoritative submissions,
56 superseded submissions, 299 processed with warnings, 117 ingestion-review
rows, no failed rows, and 415 LLM calls. Its mutable 2.4 execution overlay
preserves 2,212 succeeded actions and reconciles deterministic repairs without
executing FaCT requests.

Each check or write also creates
`out/execution-state/<run-id>.summary.json` and refreshes the latest
`out/execution_summary.json`. These mutable reports contain per-court action
outcomes, an explicit list of actions needing attention, and grouped error
themes such as pending existing-data approval, address verification, missing
accessibility details, inconsistent interview-room data, opening-hours
constraints, API validation, and authentication failures. They intentionally
contain no action bodies or raw form data. The `attention_by_request_type`
section is the product-decision report: it distinguishes
`target_replacement` (the compatibility code for existing-data approval),
`address_review`, `missing_or_invalid_form_data`,
`invalid_form_data`, `api_rejection`, and `execution_uncertain` outcomes.
`blocked` means the importer did not attempt a write. `failed` means a request
was sent and FaCT rejected it. A dependent numeric field reported as required
can either be blank or contain submitted text that could not be converted to
the API integer type; inspect the archived action evidence before approving a
placeholder.

Business-report schema 1.2 also records the deterministic migration themes:
omitted unusable counter hours, explicit no-counter-service answers, safely
recovered times, omitted invalid optional contact values, harmless punctuation
normalisation, and manually selected OS candidates. Reports contain counts,
court examples and reviewer rationale but no raw values or request bodies.

`run_manifest.json` records the source display name, completion time, run
summary, and SHA-256 hashes of every archived artifact. Use it to identify a
historic run and verify its files.

`out/execution-state/` is a local, git-ignored execution ledger and execution
report directory rather than an archive artifact. It lets the UI show whether
individual actions have been checked, blocked, completed, failed, or have an
unknown outcome without changing the generated run evidence or its integrity
hashes. Other mutable, versioned sidecars are:

- `out/llm-approval-state/<run-id>.json` for manual/policy value approvals
- `out/execution-review-state/<run-id>.json` for live submitted/effective
  comparisons and hash-bound existing-data change approvals
- `out/execution-review-state/<run-id>.plan.json` for a derived current-run
  section-plan overlay when the immutable archive predates the current manifest
- `out/.execution-jobs/<job-id>.json` for queued, running, completed, failed or
  interrupted comparison/execution jobs

The current-run overlay never edits archive evidence. Legacy section actions
that already succeeded are preserved and are never regrouped or invalidated.
hashes.

`failed_records.json` contains records that cannot progress because required
data is missing or schema-critical validation failed.

`duplicate_forms_review.xlsx` keeps every competing form together for audit.
The importer automatically selects the latest completed form, falling back to
last-modified, start time and then the highest source row. The workbook and
`submission_selection.json` show the authoritative and superseded rows; older
rows are not review blockers and cannot produce API actions.

The `Duplicate form data` tab is self-contained: it has one row for each
competing form, with the duplicate group, source row number, timestamps, the
original non-empty submitted answers, and readable cleaned summaries of
facilities, translation services, addresses, counter service, interview rooms,
contacts, and court opening hours. It is the primary tab for deciding between
duplicates and does not require `nsu_cleaned_review.xlsx`; the latter can still
be used for a wider review of validation issues if useful.

`records_needing_human_review.json` contains authoritative records blocked from
automatic import by issues such as invalid populated postcodes,
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
submissions, model name, address-verification enabled/count/cache/review and
address-action-blocking metrics, direct LLM review-row counts, API-readiness
ready/pending action counts, and the count of visible request-only migration
defaults. It also records how many API actions and LLM results initially await
field-level approval; the mutable execution summary reports the current pending
and approved counts. High-confidence address selections are automatically
approved when the model selected a supplied OS candidate with high confidence,
did not request review, and the result has an actionable address API dependency.
The selected UPRN must be present in the exact candidate list; this applies to
single- and multi-candidate lookups. The
versioned approval ledger records these as policy approvals; multi-candidate,
medium/low-confidence, null and conflicting results remain manual or read-only. Exact
unchanged high-confidence fields use a separate policy, including when another
blocker means no API action currently depends on the result.
For sparse submitted addresses, the prompt tells the model that every candidate
already came from the submitted postcode lookup. Consistent line 2 and town
evidence may be combined with a uniquely plausible OS organisation/building
name identifying a court or tribunal. The generic institutional word alone is
never sufficient, and multiple plausible or conflicting candidates remain for
review. The postcode and court slug stay outside the model request.
Automatic approval never executes a FaCT request, and address execution still
performs the fresh-UPRN, target-section and snapshot-bound effective-change preflights. In a
normal run, `vocabulary_source` should be
`fact_data_api`. The CLI prints duplicate and LLM metrics at the end of each
run. The summary separately records source submission, authoritative
submission and superseded duplicate counts. Superseded rows remain in immutable
audit evidence but do not contribute to operational review or execution.

The LLM actions page also supports an explicit `Deny and continue` decision.
Denied values are excluded from `Approve all remaining results`, do not count as
pending human review, never satisfy an execution dependency, and can be returned
to pending with `Reconsider`. Bulk approval is atomic across every LLM review
page and includes only remaining usable pending results; it excludes denied,
manual OS-candidate selections, overlength explanation repairs, read-only,
approved and already-executed results and never performs a FaCT API request.
These decisions are stored in backward-compatible approval-ledger schema 1.6,
while execution-summary schema 2.0 reports denied result and court hold counts
separately.

Rows whose submitted court slug does not exist can select a different existing
FaCT court from either the LLM review or record-audit page. The reviewer enters
the exact slug; the importer performs a read-only FaCT lookup and displays the
returned court name, canonical slug and UUID before deriving provisional
actions. It never selects a fuzzy suggestion automatically and never creates a
court. A target already used by another authoritative source row is rejected so
distinct submissions cannot be silently combined. The immutable submitted slug
and issues remain archived; the validated choice is stored in execution-review
ledger 1.2. Changing it is blocked after a write succeeds and otherwise
invalidates only comparisons and target approvals belonging to that source row.
Unrelated comparison snapshots and approvals are preserved.

Operational status counts cover authoritative submissions. Source and
superseded counts are reported separately so the original workbook row total
remains auditable.

`llm_review_submission_count` counts rows with an LLM-specific issue that
contributes to `needs_human_review`. Address verification is deliberately
separate: `address_verification_action_blocking_submission_count` counts rows
with an address action held by FaCT/OS evidence. An OS-held address does not,
by itself, change the row's import status.

`nsu_cleaned_review.xlsx` is a reviewer-friendly Excel workbook. It is not the
machine-readable source of truth; the JSON files remain that. The workbook helps
NSU/product reviewers inspect what deterministic cleaning and validation did
without recreating the original 204-column Microsoft Forms export. It contains
tabs for summary counts, processed records, records needing human review, failed
records, duplicate courts, cleaned addresses, cleaned contacts, cleaned opening
hours, address verification, flat issues, and submitter users. The record tabs include
`review_reason` and `suggested_next_action` columns. For controlled-list
failures, `review_reason` identifies the specific field and submitted value that
did not match, while the `Issues` tab provides one row per issue with raw and
cleaned values. The `Duplicate courts` tab includes the duplicate source rows,
completion/start/last-modified dates, submitter names and emails, and a
`candidate_most_recent_row` based on the available form timestamps. That row is
now the authoritative submission used operationally; the other rows remain
visible only as immutable audit evidence.

### 7. Review archived runs locally

Start the local UI after at least one completed run:

```bash
python3 -m fact_form_importer serve --output "./out"
```

The landing page lists every valid `out/final/<run_id>/run_manifest.json` from
newest to oldest. Its live `Unique courts needing review` column deduplicates
outstanding value decisions, source corrections, confirmed existing-data
approvals, ambiguous comparisons and invalid planned sections by authoritative
court. It links to `/records?work=outstanding`, which can include processed rows
with a pending model-derived value; `status=needs_human_review` remains the
immutable ingestion-result filter. Each run has a primary `/workflow` page with three guided,
non-locking steps: review LLM/address values, refresh and approve FaCT changes,
then review and execute courts. `/courts` lists one authoritative row per court
and links to `/courts/<court_slug>`, the canonical per-court comparison and
execution page. Source record pages remain available for audit, including a
plain superseded-row explanation. The Records view is also the complete
source-review checklist: every issue has a plain-English explanation and is
labelled as a Step 1 decision, a source-workbook correction requiring a fresh
run, an already-handled decision, or an information-only note. Where a row has
planned section actions it shows the count and links directly to the canonical
court workspace; where no action is possible it says so explicitly. Internal
issue codes remain available only inside expandable technical evidence. The
court page shows request bodies, relevant
cleaned values, mapped raw values, preflight outcomes and current execution
state. The court page can check target sections, inspect
request-only migration assumptions, and run one action or all safe actions for
that court. The run page also offers run-level execution. Write buttons appear only when
`FACT_DATA_API_WRITES_ENABLED=true`; server-side checks enforce the same rule.
The run-level write control prompts for confirmation and uses the same
value-approval and snapshot-bound effective-change gates as the court controls.
Action-, court-, run-, and comparison scans share one persistent background
queue. While a job is active every execution control is disabled; the execution
summary polls safe JSON progress, refreshes action/court outcomes, and becomes
the final results page. Jobs left active across a server restart are marked
`interrupted` and unknown writes are never retried automatically. Tables support status/court-row
filtering and pagination. When relevant, the run list and run page show direct
LLM review rows and OS-held address rows separately, each linking to a paginated
factor page. The per-run LLM actions page separately lists normalised fields and
addresses, shows raw/request/OS/model evidence, labels automatic and manual
approvals, and provides one approval button per pending actionable result. Use
`LLM actions review` for approval controls; `LLM review factors` is a separate
read-only explanation of why rows need review. The per-run Review overview
separates overlapping form-row blocker counts from API hold work items and links
to category queues. LLM rows show highlighted submitted-versus-proposed text;
addresses mark line one as a weaker matching signal; API changes show current
live data, the submitted proposal, the effective merged result, preserved
values, component differences and resulting POST/PUT operations.
The run page separates immutable form-ingestion results from live review
progress and API execution. Review-decision counts include approvable results
without an API action, while execution-dependency counts include only results
holding planned sections. Current-state summaries are regenerated from the
mutable approval, comparison and execution ledgers by review/execution
mutations; approving a result never rewrites archived ingestion counts. The
workflow landing page reads that atomically refreshed summary sidecar instead
of rebuilding all LLM evidence and thousands of section comparisons merely to
display its counters. It rebuilds only when the sidecar is absent or belongs to
an older summary schema. The detailed LLM queue also loads the execution-review
sidecar once per request and reuses an indexed action-to-review mapping; it does
not reload the complete comparison ledger for every displayed dependency.
The FaCT-change page labels the full action count as planned sections awaiting
one read-only scan, not as an approval count. Its summary distinguishes
unchecked, unchanged, empty-target, approval-required, approved and ambiguous
sections. Previous/next controls retain the active hold filter and individual
checks/approvals retain the current page. FaCT 401/403 responses are shown as
actionable authentication errors instead of internal server errors.
Policy-approved addresses do not contribute to “LLM approvals pending”. LLM factors are model-specific
causes of human review; OS factors hold only the affected address action. Each
run has a single ZIP download containing
its JSON, workbooks, reports, and immutable manifest; individual files remain
available in a collapsed list. The upload
form accepts CSV/XLSX only, runs one job at a time, deletes the original upload
on completion, and offers optional address verification when FaCT credentials
are configured and LLM use only as an explicit checkbox when `LLM_ENABLED=true`.

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
checked against the source of truth. The base validator does not call Ordnance
Survey or the LLM.

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

`run --verify-addresses` adds the later API-backed address stage after this
validation. It uses FaCT's OS proxy to produce auditable candidate evidence.
Only a unique, very-high-confidence match is auto-normalised. Ambiguous or
unavailable results are never guessed; an ambiguous address blocks its own API
action and remains visible in the dedicated review report/workbook tab.

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
  The raw value and warning are preserved, but the optional invalid value is
  omitted from FaCT request bodies. An otherwise-empty proposed contact item is
  omitted so merge semantics preserve live data.
- `INVALID_PHONE`: a phone value could not be parsed as a possible UK phone
  number. Optional invalid phones are preserved for review.
- `INVALID_POSTCODE`: a populated address postcode does not match the expected
  UK postcode format. This blocks automatic import because address data would
  be unreliable.
- `ADDRESS_OS_NORMALISED`: FaCT/OS returned one unique, very-high-confidence
  candidate and the importer safely mapped its address fields. The pre-OS
  cleaned address and the OS candidate remain in the address-verification
  report for audit; raw spreadsheet evidence remains in `submissions_raw.json`.
- `ADDRESS_OS_VERIFIED`: the submitted address already matched a unique OS
  candidate, so it was not changed.
- `ADDRESS_OS_REVIEW_REQUIRED`: the postcode lookup returned candidates but no
  unique high-confidence address match. No address data was changed and only
  that address action is held for review. PO Box addresses are not special
  cased: they pass through this same lookup and review path.
- `ADDRESS_OS_LOOKUP_UNAVAILABLE`: FaCT's OS proxy could not be reached or
  returned an unexpected response. No address data was changed; retry the
  check later rather than treating it as a bad address.
- `INVALID_TIME`: an opening-hours value could not be parsed as a valid `HH:MM`
  time.
- `TIME_FORMAT_RECOVERED`: one unambiguous `HH:MM` value was recovered from
  recognised formatting noise. It records the original and repaired values and
  is non-blocking.
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

## Reliability and full-run workflow (2026-07-22)

The current end-to-end workbook is `input/input-csv-4.xlsx` (472 submissions,
plus its header). This work is importer-only: it does not require or authorise
changes to `fact-data-api`.

- Scottish postcodes, including `AB`, `EH` and `G`, pass both importer manifest
  validation and the FaCT/OS lookup path. `BT`, `IM`, `JE` and `GY` remain
  unsupported.
- Invalid optional phone values remain in audit evidence as warnings but are
  omitted from API bodies. When FaCT conditionally requires a support phone for
  a false lift or accessible-entrance answer, only the outgoing request uses
  `+44 0000000000`; a valid submitted phone is always preserved.
- Counter-service `appointmentContact` is an email-only FaCT field despite its
  generic name. A supplied phone or malformed email remains in audit evidence
  with a warning, but the request omits it and sets `appointmentNeeded=false`
  rather than sending a body the API will reject.
- Lift dimensions accept metric, imperial and embedded values, choose the
  smallest reported measurement, and use the API minimum `1` only when the form
  confirms a lift but contains no usable measurement. Interview-room counts are
  read from digits, number words and explicit groups, with `1` used only when
  rooms are confirmed and no count can be recovered.
- `00:00–00:00` means closed and is omitted. No opening hours are invented when
  all periods are closed or unusable.
- Contact explanations are constrained to FaCT's 250-character/character-set
  contract. Repairs from an overlength source always require manual review.
  Reviewers can edit the exact outgoing text or omit the optional value; every
  denial or omission requires and records a reviewer-entered reason.
- Each proposed collection entry has a stable source-item ID and every API-backed
  run archives `fact_vocabularies.json`. Exact duplicate items collapse
  automatically. Complementary contact entries merge only when their populated
  fields do not conflict. Conflicts remain in Step 2, where each entry can be
  retained, remapped to an archived type, or omitted with a reason.
- An unmatched authoritative court can be closed as unactionable with a reason.
  This removes it from live outstanding-work totals and suppresses unsucceeded
  actions without changing the immutable run archive.

Mutable execution state uses run-scoped advisory file locks, unique
same-directory temporary files, atomic replacement and last-known-good backups.
The execution-review loader can recover a schema-valid leading JSON object from
a file with trailing corruption and keeps the original as `.corrupt-*` evidence.
Execution and comparison jobs share one cross-process global lock with owner,
heartbeat, progress and timing state, so two UI processes cannot run competing
jobs.

Per-write court checkpoints are stored as small atomic files under
`execution-state/<run-id>.courts/`. Reads overlay those newer shards on the
legacy/consolidated ledger, and a later consolidated save supersedes older
shards. This retains immediate durability without serialising four workers on a
full multi-thousand-action JSON rewrite for every request.

`FACT_API_EXECUTION_CONCURRENCY` controls court-level write concurrency (default
`4`, range `1`–`8`). Actions for the same court remain sequential, fresh court,
section and UPRN checks still run immediately before writes, and unknown writes
are never retried automatically. Action state is saved immediately before and
after writes; summaries are checkpointed rather than rebuilt after every court.

API-backed CLI runs automatically prepare the read-only FaCT comparisons after
archive publication. UI imports start the same retryable comparison job in the
background. The results page links to `/runs/<run-id>/business-report`, with
downloadable Markdown, CSV and JSON versions. These reports separate terminal
action success from acceptance among requests actually sent, distinguish API
latency from importer persistence time, group common themes, include reviewer
rationale and recommended decisions, and exclude raw values/request bodies.

## Review UI counts and wording

The UI deliberately separates three different units which previously appeared
under the ambiguous label "rows":

- **Source submissions** are every non-header workbook row. For the current
  `input-csv-4.xlsx` this is 472.
- **Authoritative submissions** are the latest retained forms after duplicate
  suppression. **Superseded submissions** are the older duplicates, and those
  two figures always add up to source submissions.
- **Planned courts** and **section actions** are execution units. One court can
  have several actions, so neither number should be compared directly with the
workbook-row total.

The archived-runs landing page contains an open “What the columns mean” guide.
It states explicitly that the API plan contains only courts with a currently
confirmed FaCT target and can grow when an unmatched authoritative submission
is assigned the correct target slug.

The four immutable ingestion results (processed, warnings, needs source review
and failed) add up to authoritative submissions. Live human-review totals are a
separate, deduplicated view of outstanding value decisions, source corrections,
existing-data decisions and ambiguous comparisons; they change as reviewers
record decisions.

Step 2 is a read-only comparison until a reviewer records a decision. Its
checked outcomes are mutually exclusive: already identical, empty target,
live-data approval required/approved, or ambiguous. Unchecked sections are
reported separately. Step 3 is explicitly labelled **Execute available API
actions**: its buttons perform FaCT writes when writes are enabled, while
"View court actions" links are navigation only. The Results page reports the
full manifest plan, not only actions that already have execution-ledger entries.

Archived issue, LLM-factor and OS-factor pages are labelled as immutable audit
evidence and do not imply that every row still needs action. Current reviewer
work belongs in the workflow, value-decision, live-data-decision and court-action
pages. Execution-summary schema 2.3 also counts manually selectable OS candidates
consistently with the detailed value queue and avoids reloading mutable review
state once per action when rebuilding the summary. A denied value is reported as
a denial hold, not simultaneously as a pending approval.

For conflicting address, contact or opening-hours collections, Step 2 retains
the individual retain/remap/omit controls and also provides a clearly labelled
testing aid to omit every submitted entry in that one section. The bulk decision
requires one rationale, writes all item decisions atomically, invalidates that
section's stale comparison/approval and never calls FaCT. It does not omit other
sections for the court; existing live collection entries remain protected by
merge semantics. Step 2 loads archived vocabulary choices once per resource per
request rather than once per planned action, keeping resolution redirects usable
on large runs.

Both decision pages also expose clearly labelled testing-only fast-forward
controls. Step 1 approves ordinary pending values, uses the first safely mappable
UPRN from the immutable supplied OS candidate list for unresolved addresses, and
accepts a displayed explanation only when it already fits the API limit. Step 2
approves checked, unambiguous populated-target changes and omits every submitted
item in safely editable duplicate-type collection sections with an auditable
testing rationale. Decisions are persisted atomically and proposal changes
trigger a fresh read-only comparison scan. These controls never execute a FaCT
write, never override an explicit denial, and skip missing candidates, invalid
source values, unmatched courts, unsafe conflicts, and running, succeeded or
unknown actions. Normal production review remains one decision at a time.

Current verification: 440 unit tests pass, Ruff is clean, global coverage is
90.10%, and the configured cleaners, ingestion, validators, API-manifest and web
coverage groups all meet their exact 95% gates.
