"""Apply selected LLM normalisation results to court submissions safely."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from fact_form_importer.config import AppConfig, FieldRulesConfig
from fact_form_importer.llm.client import LlmResponseParseError, normalise_fields_with_llm
from fact_form_importer.llm.normalise import (
    allowed_vocabularies_for_llm_fields,
    contains_embedded_sensitive_value,
    field_rules_for_llm_fields,
    select_llm_fields,
    vocabulary_name_for_field_path,
)
from fact_form_importer.llm.review import field_review_id
from fact_form_importer.llm.schemas import (
    LlmField,
    LlmNormalisationRequest,
    LlmNormalisationResponse,
)
from fact_form_importer.models.court_submission import CourtSubmission, sync_cleaned_snapshot
from fact_form_importer.models.issues import Issue
from fact_form_importer.validators.base import (
    LLM_LOW_CONFIDENCE,
    LLM_NORMALISATION_FAILED,
    LLM_RETURNED_INVALID_VALUE,
    LLM_RETURNED_INVALID_VOCAB_VALUE,
    LLM_RETURNED_SENSITIVE_VALUE,
    LLM_RETURNED_UNEXPECTED_FIELD,
    LLM_REVIEW_REQUIRED,
    add_issue_once,
)
from fact_form_importer.validators.vocabularies import Vocabularies
from fact_form_importer.validators.os_addresses import AddressVerificationBatch

LLM_FIELD_NORMALISED = "LLM_FIELD_NORMALISED"
# These codes deliberately remain outside the validator's human-review codes.
# Azure requires the aggregate response fields, but they cannot safely identify
# which source field needs review. Field-level results and address suggestions
# carry that decision instead.
LLM_RESPONSE_LOW_CONFIDENCE = "LLM_RESPONSE_LOW_CONFIDENCE"
LLM_RESPONSE_REVIEW_ADVISORY = "LLM_RESPONSE_REVIEW_ADVISORY"
LLM_MODEL_NOTE = "LLM_MODEL_NOTE"


@dataclass
class LlmUsageMetrics:
    calls: int = 0
    failures: int = 0
    retries: int = 0
    fields_selected: int = 0
    fields_processed: int = 0
    submissions_with_selected_fields: int = 0
    address_candidate_groups_selected: int = 0
    address_suggestions_recorded: int = 0

    def as_dict(self, model: str | None = None) -> dict[str, int | str | None]:
        return {
            "llm_calls": self.calls,
            "llm_failures": self.failures,
            "llm_retries": self.retries,
            "llm_fields_selected": self.fields_selected,
            "llm_fields_processed": self.fields_processed,
            "llm_submissions_with_selected_fields": self.submissions_with_selected_fields,
            "llm_address_candidate_groups_selected": self.address_candidate_groups_selected,
            "llm_address_suggestions_recorded": self.address_suggestions_recorded,
            "llm_model": model,
        }


@dataclass
class LlmNormalisationResult:
    submissions: list[CourtSubmission]
    metrics: LlmUsageMetrics
    review_items: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class _PreparedLlmCall:
    submission: CourtSubmission
    fields: list[LlmField]
    request: LlmNormalisationRequest


@dataclass(frozen=True)
class _LlmCallOutcome:
    response: LlmNormalisationResponse | None
    error: Exception | None
    calls: int
    retries: int


Normaliser = Callable[[LlmNormalisationRequest, Optional[AppConfig]], LlmNormalisationResponse]


def normalise_submissions_with_llm(
    submissions: list[CourtSubmission],
    field_rules: FieldRulesConfig,
    vocabularies: Vocabularies | None,
    config: AppConfig | None = None,
    normaliser: Normaliser = normalise_fields_with_llm,
    address_verifications: AddressVerificationBatch | None = None,
) -> LlmNormalisationResult:
    """Run scoped LLM requests concurrently, with at most one request per row.

    Calls are isolated from each other, while results are applied in source order
    after the requests finish. This avoids a long serial run without combining
    form rows or exposing one row's data to another request.
    """

    app_config = config or AppConfig()
    metrics = LlmUsageMetrics()
    review_items: list[dict[str, object]] = []
    prepared_calls: list[_PreparedLlmCall] = []

    for submission in submissions:
        fields = select_llm_fields(submission, field_rules, vocabularies)
        address_candidates = (
            address_verifications.llm_candidates_for(submission) if address_verifications else []
        )
        if not fields and not address_candidates:
            continue

        metrics.submissions_with_selected_fields += 1
        metrics.fields_selected += len(fields)
        metrics.address_candidate_groups_selected += len(address_candidates)
        request = build_llm_request(
            submission,
            fields,
            field_rules,
            vocabularies,
            address_candidates=address_candidates,
        )

        prepared_calls.append(
            _PreparedLlmCall(
                submission=submission,
                fields=fields,
                request=request,
            )
        )

    if not prepared_calls:
        return LlmNormalisationResult(
            submissions=submissions, metrics=metrics, review_items=review_items
        )

    max_workers = min(app_config.llm_max_concurrency, len(prepared_calls))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="fact-llm") as executor:
        outcomes = list(
            executor.map(
                lambda prepared: _call_with_one_parse_retry(
                    prepared.request,
                    app_config,
                    normaliser,
                ),
                prepared_calls,
            )
        )

    for prepared, outcome in zip(prepared_calls, outcomes):
        submission = prepared.submission
        review_start = len(review_items)
        metrics.calls += outcome.calls
        metrics.retries += outcome.retries

        if outcome.error is not None:
            metrics.failures += 1
            add_issue_once(
                submission,
                Issue(
                    field="llm",
                    code=LLM_NORMALISATION_FAILED,
                    severity="warning",
                    message="LLM normalisation failed; this row requires human review",
                    raw_value=None,
                    cleaned_value={"error_type": type(outcome.error).__name__},
                ),
            )
            for field in prepared.fields:
                review_items.append(
                    _field_review_item(
                        submission,
                        field,
                        value=None,
                        operation="unresolved",
                        confidence="unavailable",
                        needs_human_review=True,
                        reason=f"LLM request failed ({type(outcome.error).__name__})",
                        outcome="failed",
                    )
                )
            _attach_exact_request(review_items[review_start:], prepared.request)
            continue

        if outcome.response is None:
            raise AssertionError("A successful LLM call must include a response")

        metrics.fields_processed += apply_llm_response(
            submission,
            outcome.response,
            prepared.fields,
            vocabularies,
            address_verifications=address_verifications,
            metrics=metrics,
            review_items=review_items,
        )
        _attach_exact_request(review_items[review_start:], prepared.request)

    return LlmNormalisationResult(
        submissions=submissions, metrics=metrics, review_items=review_items
    )


def apply_llm_response(
    submission: CourtSubmission,
    response: LlmNormalisationResponse,
    selected_fields: list[LlmField],
    vocabularies: Vocabularies | None,
    address_verifications: AddressVerificationBatch | None = None,
    metrics: LlmUsageMetrics | None = None,
    review_items: list[dict[str, object]] | None = None,
) -> int:
    """Apply only safe, selected, schema-valid LLM values to a submission."""

    if response.record_id != _record_id(submission):
        add_issue_once(
            submission,
            Issue(
                field="llm",
                code=LLM_NORMALISATION_FAILED,
                severity="warning",
                message="LLM response record identifier did not match the submitted row",
                raw_value=None,
                cleaned_value=None,
            ),
        )
        return 0

    selected_by_path = {field.field: field for field in selected_fields}
    processed = 0
    for normalised_field in response.normalised_fields:
        selected = selected_by_path.get(normalised_field.field)
        if selected is None:
            _add_llm_issue(
                submission,
                normalised_field.field,
                LLM_RETURNED_UNEXPECTED_FIELD,
                "LLM returned a field that was not selected for normalisation",
                normalised_field.value,
            )
            if review_items is not None:
                review_items.append(
                    {
                        "review_id": field_review_id(
                            submission.source.source_row_number,
                            normalised_field.field,
                        ),
                        "kind": "field",
                        "source_row_number": submission.source.source_row_number,
                        "court_slug": submission.court_slug,
                        "field": normalised_field.field,
                        "llm_input": None,
                        "model_result": normalised_field.model_dump(mode="json"),
                        "outcome": "unexpected_field",
                    }
                )
            continue

        if normalised_field.operation == "unresolved":
            if normalised_field.value is not None:
                _add_llm_issue(
                    submission,
                    selected.field,
                    LLM_RETURNED_INVALID_VALUE,
                    "LLM unresolved operation must have a null value",
                    normalised_field.value,
                )
                continue
            # The model must scope an unresolved selected field here rather
            # than relying on the aggregate response-level review flag.
            if normalised_field.needs_human_review:
                _add_llm_issue(
                    submission,
                    selected.field,
                    LLM_REVIEW_REQUIRED,
                    "LLM marked this field as requiring human review",
                    normalised_field.reason,
                )
            if review_items is not None:
                review_items.append(
                    _field_review_item(
                        submission,
                        selected,
                        value=None,
                        operation="unresolved",
                        confidence=normalised_field.confidence,
                        needs_human_review=normalised_field.needs_human_review,
                        reason=normalised_field.reason,
                        outcome="no_value",
                    )
                )
            continue

        if normalised_field.operation == "clear":
            if (
                selected.field.split("]", 1)[-1].lstrip(".") != "explanation"
                or not selected.field.startswith("contacts[")
                or normalised_field.value is not None
                or normalised_field.needs_human_review
            ):
                _add_llm_issue(
                    submission,
                    selected.field,
                    LLM_RETURNED_INVALID_VALUE,
                    "LLM clear operation is not permitted for this field or response state",
                    normalised_field.value,
                )
                if review_items is not None:
                    review_items.append(
                        _field_review_item(
                            submission,
                            selected,
                            value=normalised_field.value,
                            operation="clear",
                            confidence=normalised_field.confidence,
                            needs_human_review=True,
                            reason=normalised_field.reason,
                            outcome="rejected",
                        )
                    )
                continue
            if not _set_selected_value(submission, selected.field, None):
                _add_llm_issue(
                    submission,
                    selected.field,
                    LLM_RETURNED_UNEXPECTED_FIELD,
                    "LLM field path could not be cleared on this submission",
                    None,
                )
                continue
            processed += 1
            if review_items is not None:
                review_items.append(
                    _field_review_item(
                        submission,
                        selected,
                        value=None,
                        operation="clear",
                        confidence=normalised_field.confidence,
                        needs_human_review=False,
                        reason=normalised_field.reason,
                        outcome="accepted",
                    )
                )
            add_issue_once(
                submission,
                Issue(
                    field=selected.field,
                    code=LLM_FIELD_NORMALISED,
                    severity="info",
                    message="LLM cleared an optional selected field",
                    raw_value=selected.cleaned_value,
                    cleaned_value=None,
                ),
            )
            continue

        if normalised_field.operation != "set" or normalised_field.value is None:
            _add_llm_issue(
                submission,
                selected.field,
                LLM_RETURNED_INVALID_VALUE,
                "LLM set operation must have a non-null value",
                normalised_field.value,
            )
            continue

        value = _safe_value_for_field(
            submission,
            selected,
            normalised_field.value,
            vocabularies,
        )
        if value is _REJECTED:
            if review_items is not None:
                review_items.append(
                    _field_review_item(
                        submission,
                        selected,
                        value=normalised_field.value,
                        operation=normalised_field.operation,
                        confidence=normalised_field.confidence,
                        needs_human_review=True,
                        reason=normalised_field.reason,
                        outcome="rejected",
                    )
                )
            continue

        if not _set_selected_value(submission, selected.field, value):
            _add_llm_issue(
                submission,
                selected.field,
                LLM_RETURNED_UNEXPECTED_FIELD,
                "LLM field path could not be applied to this submission",
                normalised_field.value,
            )
            if review_items is not None:
                review_items.append(
                    _field_review_item(
                        submission,
                        selected,
                        value=normalised_field.value,
                        operation=normalised_field.operation,
                        confidence=normalised_field.confidence,
                        needs_human_review=True,
                        reason=normalised_field.reason,
                        outcome="unapplied",
                    )
                )
            continue

        processed += 1
        if review_items is not None:
            review_items.append(
                _field_review_item(
                    submission,
                    selected,
                    value=value,
                    operation="set",
                    confidence=normalised_field.confidence,
                    needs_human_review=normalised_field.needs_human_review,
                    reason=normalised_field.reason,
                    outcome="accepted",
                )
            )
        add_issue_once(
            submission,
            Issue(
                field=selected.field,
                code=LLM_FIELD_NORMALISED,
                severity="info",
                message="LLM normalised a selected field",
                raw_value=selected.cleaned_value,
                cleaned_value=value,
            ),
        )

        if normalised_field.confidence in {"medium", "low"}:
            _add_llm_issue(
                submission,
                selected.field,
                LLM_LOW_CONFIDENCE,
                "LLM normalisation confidence was not high",
                normalised_field.confidence,
            )
        if normalised_field.needs_human_review:
            _add_llm_issue(
                submission,
                selected.field,
                LLM_REVIEW_REQUIRED,
                "LLM marked this field as requiring human review",
                normalised_field.reason,
            )

    _record_response_advisories(submission, response)

    for issue in response.issues:
        issue_field = issue.field if issue.field in selected_by_path else "llm"
        add_issue_once(
            submission,
            Issue(
                field=issue_field,
                # Model-authored issue codes must not change status by
                # colliding with importer-owned validation codes. Field-level
                # confidence/review flags above are the only model signals
                # that can hold a record for review.
                code=LLM_MODEL_NOTE,
                severity="info",
                message=f"LLM note ({issue.code}): {issue.message}",
                raw_value=issue.code,
                cleaned_value={"model_severity": issue.severity},
            ),
        )

    _record_address_suggestions(
        submission,
        response,
        address_verifications,
        metrics,
    )

    sync_cleaned_snapshot(submission)
    return processed


def _field_review_item(
    submission: CourtSubmission,
    selected: LlmField,
    *,
    value: object,
    operation: str,
    confidence: str,
    needs_human_review: bool,
    reason: str,
    outcome: str,
) -> dict[str, object]:
    row = submission.source.source_row_number
    return {
        "review_id": field_review_id(row, selected.field),
        "kind": "field",
        "source_row_number": row,
        "court_slug": submission.court_slug,
        "field": selected.field,
        "llm_input": {
            "raw_value": selected.raw_value,
            "cleaned_value": selected.cleaned_value,
        },
        "model_result": {
            "value": value,
            "operation": operation,
            "confidence": confidence,
            "needs_human_review": needs_human_review,
            "reason": reason,
        },
        "outcome": outcome,
    }


def _attach_exact_request(
    items: list[dict[str, object]], request: LlmNormalisationRequest
) -> None:
    exact_request = request.model_dump(mode="json")
    for item in items:
        item["llm_request"] = exact_request


_REJECTED = object()


def _call_with_one_parse_retry(
    request: LlmNormalisationRequest,
    config: AppConfig,
    normaliser: Normaliser,
) -> _LlmCallOutcome:
    calls = 0
    retries = 0
    for attempt in range(2):
        calls += 1
        try:
            return _LlmCallOutcome(
                response=normaliser(request, config),
                error=None,
                calls=calls,
                retries=retries,
            )
        except LlmResponseParseError as exc:
            if attempt == 0:
                retries += 1
                continue
            return _LlmCallOutcome(
                response=None,
                error=exc,
                calls=calls,
                retries=retries,
            )
        except Exception as exc:
            return _LlmCallOutcome(
                response=None,
                error=exc,
                calls=calls,
                retries=retries,
            )

    raise AssertionError("LLM parse retry loop should always return or raise")


def build_llm_request(
    submission: CourtSubmission,
    fields: list[LlmField],
    field_rules: FieldRulesConfig,
    vocabularies: Vocabularies | None,
    *,
    address_candidates: list[dict[str, object]] | None = None,
) -> LlmNormalisationRequest:
    return LlmNormalisationRequest(
        record_id=_record_id(submission),
        source_row_number=submission.source.source_row_number,
        # A slug is deliberately not sent to the model, even though the schema
        # keeps this optional field for manual, synthetic LLM tests.
        court_slug=None,
        fields=fields,
        allowed_vocabularies=allowed_vocabularies_for_llm_fields(fields, vocabularies),
        field_rules=field_rules_for_llm_fields(fields, field_rules),
        address_candidates=address_candidates or [],
    )


def build_llm_request_review(
    submissions: list[CourtSubmission],
    field_rules: FieldRulesConfig,
    vocabularies: Vocabularies | None,
    address_verifications: AddressVerificationBatch | None = None,
) -> list[LlmNormalisationRequest]:
    """Build inspectable request payloads without calling the model."""

    requests: list[LlmNormalisationRequest] = []
    for submission in submissions:
        fields = select_llm_fields(submission, field_rules, vocabularies)
        address_candidates = (
            address_verifications.llm_candidates_for(submission) if address_verifications else []
        )
        if fields or address_candidates:
            requests.append(
                build_llm_request(
                    submission,
                    fields,
                    field_rules,
                    vocabularies,
                    address_candidates=address_candidates,
                )
            )
    return requests


def _record_id(submission: CourtSubmission) -> str:
    return f"source-row-{submission.source.source_row_number}"


def _safe_value_for_field(
    submission: CourtSubmission,
    selected: LlmField,
    value: str | list[str],
    vocabularies: Vocabularies | None,
) -> str | list[str] | object:
    vocabulary_name = vocabulary_name_for_field_path(selected.field)
    if vocabulary_name is not None:
        return _canonicalise_vocab_value(submission, selected, value, vocabulary_name, vocabularies)

    if not isinstance(value, str) or not value.strip():
        _add_llm_issue(
            submission,
            selected.field,
            LLM_RETURNED_INVALID_VALUE,
            "LLM returned an empty or non-text value for a public text field",
            value,
        )
        return _REJECTED
    if contains_embedded_sensitive_value(value):
        _add_llm_issue(
            submission,
            selected.field,
            LLM_RETURNED_SENSITIVE_VALUE,
            "LLM returned a value containing email, phone, or postcode data",
            value,
        )
        return _REJECTED
    return value.strip()


def _canonicalise_vocab_value(
    submission: CourtSubmission,
    selected: LlmField,
    value: str | list[str],
    vocabulary_name: str,
    vocabularies: Vocabularies | None,
) -> str | list[str] | object:
    if vocabularies is None:
        _add_llm_issue(
            submission,
            selected.field,
            LLM_RETURNED_INVALID_VOCAB_VALUE,
            "LLM vocabulary result cannot be verified because no vocabulary is loaded",
            value,
        )
        return _REJECTED

    values = value if isinstance(value, list) else [value]
    if not values or any(not isinstance(item, str) or not item.strip() for item in values):
        _add_llm_issue(
            submission,
            selected.field,
            LLM_RETURNED_INVALID_VOCAB_VALUE,
            "LLM returned an empty or non-text controlled vocabulary value",
            value,
        )
        return _REJECTED

    canonical_values: list[str] = []
    for item in values:
        match = vocabularies.normalised_vocab_match(item, vocabulary_name)
        if match is None:
            _add_llm_issue(
                submission,
                selected.field,
                LLM_RETURNED_INVALID_VOCAB_VALUE,
                f"LLM returned a value outside vocabulary '{vocabulary_name}'",
                value,
            )
            return _REJECTED
        if match.name not in canonical_values:
            canonical_values.append(match.name)

    if isinstance(selected.cleaned_value, list):
        return canonical_values
    if len(canonical_values) != 1:
        _add_llm_issue(
            submission,
            selected.field,
            LLM_RETURNED_INVALID_VOCAB_VALUE,
            "LLM returned multiple values for a single-value controlled vocabulary field",
            value,
        )
        return _REJECTED
    return canonical_values[0]


def _set_selected_value(
    submission: CourtSubmission, path: str, value: str | list[str] | None
) -> bool:
    if path.startswith("facilities."):
        submission.facilities[path.removeprefix("facilities.")] = value
        return True
    if path == "counter_service.assists_with":
        submission.counter_service["assists_with"] = value
        return True
    if path.startswith("addresses["):
        target = _indexed_target(submission.addresses, path)
        if target is None:
            return False
        _, attribute = target
        if attribute not in {"address_type", "areas_of_law", "court_types"}:
            return False
        setattr(target[0], attribute, value)
        return True
    if path.startswith("contacts["):
        target = _indexed_target(submission.contacts, path)
        if target is None:
            return False
        _, attribute = target
        if attribute not in {"description", "explanation"}:
            return False
        if value is None and attribute != "explanation":
            return False
        setattr(target[0], attribute, value)
        return True
    if path.startswith("opening_hours["):
        target = _indexed_target(submission.opening_hours, path)
        if target is None or target[1] != "type":
            return False
        target[0].type = value if isinstance(value, str) else None
        return isinstance(value, str)
    return False


def _record_address_suggestions(
    submission: CourtSubmission,
    response: LlmNormalisationResponse,
    address_verifications: AddressVerificationBatch | None,
    metrics: LlmUsageMetrics | None,
) -> None:
    """Record advisory OS candidate rankings without changing address fields."""

    if address_verifications is None:
        return
    for match in response.address_matches:
        recorded = address_verifications.record_llm_suggestion(
            submission,
            match.address_index,
            match.uprn,
            match.confidence,
            match.needs_human_review,
            match.reason,
        )
        if recorded:
            if metrics is not None:
                metrics.address_suggestions_recorded += 1
            continue
        _add_llm_issue(
            submission,
            f"addresses[{match.address_index}]",
            LLM_RETURNED_INVALID_VALUE,
            "LLM returned an address candidate outside the supplied Ordnance Survey candidates",
            match.uprn,
        )


def _indexed_target(items: list, path: str):
    try:
        index_text, attribute = path.split("].", 1)
        index = int(index_text.split("[", 1)[1])
    except (IndexError, ValueError):
        return None

    item = next((candidate for candidate in items if candidate.index == index), None)
    return (item, attribute) if item is not None else None


def _add_llm_issue(
    submission: CourtSubmission,
    field: str,
    code: str,
    message: str,
    cleaned_value: object,
) -> None:
    add_issue_once(
        submission,
        Issue(
            field=field,
            code=code,
            severity="warning",
            message=message,
            raw_value=None,
            cleaned_value=cleaned_value,
        ),
    )


def _record_response_advisories(
    submission: CourtSubmission,
    response: LlmNormalisationResponse,
) -> None:
    """Keep aggregate model signals for audit without widening record scope.

    A row can contain several selected fields and advisory address candidates.
    The top-level response confidence does not say which one is uncertain, so
    treating it as a submission-level blocker turns otherwise importable rows
    into review records. The response schema still preserves these values for
    observability; only field-level flags control review status.
    """

    if response.confidence in {"medium", "low"}:
        add_issue_once(
            submission,
            Issue(
                field="llm",
                code=LLM_RESPONSE_LOW_CONFIDENCE,
                severity="info",
                message="LLM returned aggregate confidence below high; see field-level results",
                raw_value=None,
                cleaned_value=response.confidence,
            ),
        )
    if response.needs_human_review:
        add_issue_once(
            submission,
            Issue(
                field="llm",
                code=LLM_RESPONSE_REVIEW_ADVISORY,
                severity="info",
                message="LLM returned an aggregate review signal; see field-level results",
                raw_value=None,
                cleaned_value=None,
            ),
        )
