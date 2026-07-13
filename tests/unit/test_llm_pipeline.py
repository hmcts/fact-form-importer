from fact_form_importer.config import FieldRulesConfig
from fact_form_importer.llm.client import LlmResponseParseError
from fact_form_importer.llm.pipeline import (
    LLM_FIELD_NORMALISED,
    apply_llm_response,
    normalise_submissions_with_llm,
)
from fact_form_importer.llm.schemas import LlmAddressMatch, LlmNormalisedField, LlmNormalisationResponse
from fact_form_importer.llm.schemas import LlmField
from fact_form_importer.models.court_submission import Address, ContactDetail, CourtSubmission
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.validators.base import (
    LLM_LOW_CONFIDENCE,
    LLM_NORMALISATION_FAILED,
    LLM_RETURNED_INVALID_VOCAB_VALUE,
    LLM_RETURNED_SENSITIVE_VALUE,
    LLM_RETURNED_UNEXPECTED_FIELD,
    VOCAB_NO_MATCH,
    calculate_status,
    clear_validation_issues,
    validate_submission,
)
from fact_form_importer.validators.os_addresses import AddressVerification, AddressVerificationBatch, OsAddressCandidate
from fact_form_importer.validators.vocabularies import Vocabularies


def test_pipeline_calls_once_per_selected_submission_and_applies_canonical_vocab_value():
    submission = _submission(description="general enquiries")
    requests = []

    def normaliser(request, config):
        requests.append(request)
        return _response(
            request.record_id,
            "contacts[1].description",
            "Enquiries",
        )

    result = normalise_submissions_with_llm(
        [submission],
        _rules(),
        _vocabularies(),
        normaliser=normaliser,
    )

    assert len(requests) == 1
    assert requests[0].court_slug is None
    assert [field.field for field in requests[0].fields] == ["contacts[1].description"]
    assert requests[0].allowed_vocabularies == {
        "contacts[1].description": ["Enquiries", "Appointments"]
    }
    assert submission.contacts[0].description == "Enquiries"
    assert result.metrics.calls == 1
    assert result.metrics.failures == 0
    assert result.metrics.fields_selected == 1
    assert result.metrics.fields_processed == 1
    assert _has_issue(submission, LLM_FIELD_NORMALISED)


def test_pipeline_does_not_call_model_when_all_values_match_vocabularies():
    submission = _submission(description="Enquiries")

    def normaliser(request, config):
        raise AssertionError("normaliser should not be called")

    result = normalise_submissions_with_llm(
        [submission], _rules(), _vocabularies(), normaliser=normaliser
    )

    assert result.metrics.calls == 0
    assert result.metrics.submissions_with_selected_fields == 0


def test_pipeline_retries_once_for_unparseable_response():
    submission = _submission(description="general enquiries")
    calls = 0

    def normaliser(request, config):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LlmResponseParseError("bad JSON")
        return _response(request.record_id, "contacts[1].description", "Enquiries")

    result = normalise_submissions_with_llm(
        [submission], _rules(), _vocabularies(), normaliser=normaliser
    )

    assert calls == 2
    assert result.metrics.calls == 2
    assert result.metrics.retries == 1
    assert result.metrics.failures == 0
    assert submission.contacts[0].description == "Enquiries"


def test_pipeline_marks_only_the_failing_row_for_human_review():
    submission = _submission(description="general enquiries")

    def normaliser(request, config):
        raise RuntimeError("service unavailable")

    result = normalise_submissions_with_llm(
        [submission], _rules(), _vocabularies(), normaliser=normaliser
    )

    assert result.metrics.calls == 1
    assert result.metrics.failures == 1
    assert _has_issue(submission, LLM_NORMALISATION_FAILED)
    assert calculate_status(submission) == "needs_human_review"


def test_pipeline_rejects_vocab_value_not_owned_by_fact_vocabulary():
    submission = _submission(description="general enquiries")

    def normaliser(request, config):
        return _response(request.record_id, "contacts[1].description", "Made up category")

    result = normalise_submissions_with_llm(
        [submission], _rules(), _vocabularies(), normaliser=normaliser
    )

    assert submission.contacts[0].description == "general enquiries"
    assert result.metrics.fields_processed == 0
    assert _has_issue(submission, LLM_RETURNED_INVALID_VOCAB_VALUE)
    assert calculate_status(submission) == "needs_human_review"


def test_medium_confidence_result_requires_human_review_after_safe_merge():
    submission = _submission(description="general enquiries")

    def normaliser(request, config):
        return _response(
            request.record_id,
            "contacts[1].description",
            "Enquiries",
            confidence="medium",
        )

    normalise_submissions_with_llm([submission], _rules(), _vocabularies(), normaliser=normaliser)

    assert submission.contacts[0].description == "Enquiries"
    assert _has_issue(submission, LLM_LOW_CONFIDENCE)
    assert calculate_status(submission) == "needs_human_review"


def test_unselected_response_field_is_rejected_without_mutating_submission():
    submission = _submission(description="general enquiries")
    fields = [
        type("Selected", (), {
            "field": "contacts[1].description",
            "raw_value": "general enquiries",
            "cleaned_value": "general enquiries",
        })()
    ]
    response = _response("source-row-2", "translation_email", "should-not-be-applied")

    processed = apply_llm_response(submission, response, fields, _vocabularies())

    assert processed == 0
    assert submission.contacts[0].description == "general enquiries"
    assert _has_issue(submission, LLM_RETURNED_UNEXPECTED_FIELD)


def test_mismatched_response_identifier_is_treated_as_row_failure():
    submission = _submission(description="general enquiries")
    fields = [LlmField(field="contacts[1].description", raw_value="general enquiries")]
    response = _response("source-row-999", "contacts[1].description", "Enquiries")

    processed = apply_llm_response(submission, response, fields, _vocabularies())

    assert processed == 0
    assert submission.contacts[0].description == "general enquiries"
    assert _has_issue(submission, LLM_NORMALISATION_FAILED)


def test_public_text_value_with_contact_data_is_rejected():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        facilities={"accessible_toilet_description": "Near reception."},
    )
    fields = [
        LlmField(
            field="facilities.accessible_toilet_description",
            raw_value="Near reception.",
            cleaned_value="Near reception.",
        )
    ]
    response = _response(
        "source-row-2",
        "facilities.accessible_toilet_description",
        "Near reception. Call 020 7946 0000 for access.",
    )

    processed = apply_llm_response(submission, response, fields, _vocabularies())

    assert processed == 0
    assert submission.facilities["accessible_toilet_description"] == "Near reception."
    assert _has_issue(submission, LLM_RETURNED_SENSITIVE_VALUE)


def test_list_vocabulary_value_is_canonicalised_and_applied_to_address():
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        addresses=[Address(index=1, areas_of_law=["family cases"])],
    )
    fields = [
        LlmField(
            field="addresses[1].areas_of_law",
            raw_value=["family cases"],
            cleaned_value=["family cases"],
        )
    ]
    vocabularies = Vocabularies(
        version="test.1",
        vocabularies={
            "areas_of_law": [{"code": "family", "name": "Family"}],
        },
    )
    response = _response("source-row-2", "addresses[1].areas_of_law", ["family"])

    processed = apply_llm_response(submission, response, fields, vocabularies)

    assert processed == 1
    assert submission.addresses[0].areas_of_law == ["Family"]


def test_record_level_llm_review_flag_requires_human_review():
    submission = _submission(description="general enquiries")
    fields = [LlmField(field="contacts[1].description", raw_value="general enquiries")]
    response = _response("source-row-2", "contacts[1].description", "Enquiries")
    response.needs_human_review = True
    response.issues = []

    apply_llm_response(submission, response, fields, _vocabularies())

    assert calculate_status(submission) == "needs_human_review"


def test_revalidation_removes_stale_vocab_issue_after_valid_llm_mapping():
    submission = _submission(description="general enquiries")
    validate_submission(submission, _vocabularies())
    assert _has_issue(submission, VOCAB_NO_MATCH)

    def normaliser(request, config):
        return _response(request.record_id, "contacts[1].description", "Enquiries")

    normalise_submissions_with_llm([submission], _rules(), _vocabularies(), normaliser=normaliser)
    clear_validation_issues([submission])
    validate_submission(submission, _vocabularies())

    assert not _has_issue(submission, VOCAB_NO_MATCH)
    assert submission.status == "processed"


def test_pipeline_can_record_an_advisory_os_candidate_without_mutating_the_address():
    submission = _submission(description="Enquiries")
    submission.addresses = [
        Address(index=1, line_1="1 Main Street", town_or_city="London", postcode="SW1A 1AA")
    ]
    verification = AddressVerification(
        source_row_number=2,
        court_slug="example-court",
        address_index=1,
        postcode="SW1A 1AA",
        status="review_required",
        message="No unique high-confidence OS match was found",
        original_address=submission.addresses[0].model_dump(mode="json"),
        candidates=[
            OsAddressCandidate(
                uprn="uprn-1",
                address="1 Main Street, London",
                organisation_name=None,
                building_number="1",
                building_name=None,
                thoroughfare_name="Main Street",
                post_town="London",
                postcode="SW1A 1AA",
            )
        ],
    )
    batch = AddressVerificationBatch(enabled=True, verifications=[verification])

    def normaliser(request, config):
        assert request.fields == []
        assert request.address_candidates[0].address_index == 1
        assert "postcode" not in request.address_candidates[0].submitted_address
        return LlmNormalisationResponse(
            record_id=request.record_id,
            normalised_fields=[],
            confidence="high",
            needs_human_review=True,
            issues=[],
            address_matches=[
                LlmAddressMatch(
                    address_index=1,
                    uprn="uprn-1",
                    confidence="high",
                    needs_human_review=True,
                    reason="The supplied address best matches this candidate.",
                )
            ],
        )

    result = normalise_submissions_with_llm(
        [submission], _rules(), _vocabularies(), normaliser=normaliser, address_verifications=batch
    )

    assert submission.addresses[0].line_1 == "1 Main Street"
    assert verification.llm_suggestion["uprn"] == "uprn-1"
    assert result.metrics.address_candidate_groups_selected == 1
    assert result.metrics.address_suggestions_recorded == 1


def _submission(description):
    return CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug_raw="example-court",
        court_slug="example-court",
        contacts=[ContactDetail(index=1, description=description)],
    )


def _rules():
    return FieldRulesConfig(
        version="test.1",
        fields={
            "contact.description": {
                "llm": {
                    "enabled": True,
                    "purpose": "map_to_contact_description_type",
                    "use_only_when": "not_exact_vocab_match",
                    "rules": ["Map to exactly one allowed contact type."],
                }
            }
        },
    )


def _vocabularies():
    return Vocabularies(
        version="test.1",
        vocabularies={
            "contact_description_types": [
                {"code": "enquiries", "name": "Enquiries"},
                {"code": "appointments", "name": "Appointments"},
            ]
        },
    )


def _response(record_id, field, value, confidence="high"):
    return LlmNormalisationResponse(
        record_id=record_id,
        normalised_fields=[
            LlmNormalisedField(
                field=field,
                value=value,
                confidence=confidence,
                needs_human_review=False,
                reason="Test response.",
            )
        ],
        confidence=confidence,
        needs_human_review=False,
        issues=[],
    )


def _has_issue(submission, code):
    return any(issue.code == code for issue in submission.issues)
