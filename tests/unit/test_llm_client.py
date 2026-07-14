import json
from types import SimpleNamespace

import pytest

from fact_form_importer.config import AppConfig
from fact_form_importer.llm.client import (
    LlmResponseParseError,
    build_llm_test_request,
    normalise_fields_with_llm,
    _response_json,
)
from fact_form_importer.llm.openai_client import check_llm_connection, _response_preview
from fact_form_importer.llm.schemas import LlmNormalisationResponse


def test_check_llm_connection_calls_openai_responses_api(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ai-foundry.example.test/openai/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "token")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.5")
    calls = []

    class FakeResponses:
        def create(self, model, input):
            calls.append({"model": model, "input": input})
            return SimpleNamespace(output_text="OK")

    class FakeClient:
        def __init__(self, base_url, api_key):
            calls.append({"base_url": base_url, "api_key": api_key})
            self.responses = FakeResponses()

    result = check_llm_connection(AppConfig(), client_factory=FakeClient)

    assert result.base_url == "https://ai-foundry.example.test/openai/v1"
    assert result.model == "gpt-5.5"
    assert result.output_preview == "OK"
    assert calls == [
        {"base_url": "https://ai-foundry.example.test/openai/v1", "api_key": "token"},
        {"model": "gpt-5.5", "input": "Reply with exactly: OK"},
    ]


def test_check_llm_connection_requires_openai_config(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
        check_llm_connection(AppConfig(), client_factory=lambda **kwargs: None)


def test_response_preview_supports_output_and_fallback_shapes():
    assert _response_preview(SimpleNamespace(output=[" OK from output "])) == "OK from output"
    assert _response_preview(" OK from string ") == "OK from string"


def test_normalise_fields_with_llm_uses_structured_json_output(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ai-foundry.example.test/openai/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "token")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.5")
    calls = []

    class FakeResponses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                output_text=json.dumps(
                    {
                        "record_id": "llm-test-record",
                        "normalised_fields": [
                                {
                                    "field": "facilities.accessible_toilet_description",
                                    "operation": "set",
                                    "value": "Disabled toilet near reception. Ask security for access.",
                                "confidence": "medium",
                                "needs_human_review": True,
                                "reason": "Mentions asking security, so review is required.",
                            }
                        ],
                        "confidence": "medium",
                        "needs_human_review": True,
                        "issues": [
                            {
                                "field": "facilities.accessible_toilet_description",
                                "code": "LLM_REVIEW_REQUIRED",
                                "severity": "warning",
                                "message": "Answer says to ask security.",
                            }
                        ],
                        "address_matches": [],
                    }
                )
            )

    class FakeClient:
        def __init__(self, base_url, api_key):
            self.responses = FakeResponses()

    result = normalise_fields_with_llm(
        build_llm_test_request(),
        AppConfig(),
        client_factory=FakeClient,
    )

    assert result.record_id == "llm-test-record"
    assert result.confidence == "medium"
    assert result.needs_human_review is True
    assert calls[0]["model"] == "gpt-5.5"
    assert "temperature" not in calls[0]
    assert calls[0]["text"]["format"]["type"] == "json_schema"
    assert "accessible_toilet_description" in calls[0]["input"]
    prompt = str(calls[0]["input"])
    assert "Available on the ground floor." in prompt
    assert "Available on the ground, first and third floors." in prompt
    assert "National Contact Centre for Civil and Family Court" in prompt
    assert "operation 'clear'" in prompt


def test_llm_response_schema_requires_every_object_property_for_azure():
    schema = LlmNormalisationResponse.model_json_schema()

    assert set(schema["properties"]) == set(schema["required"])
    address_match = schema["$defs"]["LlmAddressMatch"]
    assert set(address_match["properties"]) == set(address_match["required"])
    normalised_field = schema["$defs"]["LlmNormalisedField"]
    assert "operation" in normalised_field["required"]


def test_normalise_fields_with_llm_requires_openai_config(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
        normalise_fields_with_llm(build_llm_test_request(), AppConfig(), client_factory=lambda **kwargs: None)


def test_response_json_supports_nested_output_content_and_fallback():
    nested_response = SimpleNamespace(
        output=[
            SimpleNamespace(
                content=[
                    SimpleNamespace(text='{"record_id": "nested"}')
                ]
            )
        ]
    )

    assert _response_json(nested_response) == {"record_id": "nested"}
    assert _response_json('{"record_id": "fallback"}') == {"record_id": "fallback"}


def test_normalise_fields_with_llm_wraps_invalid_structured_responses(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ai-foundry.example.test/openai/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "token")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.5")

    class FakeResponses:
        def create(self, **kwargs):
            return SimpleNamespace(output_text="not-json")

    class FakeClient:
        def __init__(self, base_url, api_key):
            self.responses = FakeResponses()

    with pytest.raises(LlmResponseParseError, match="expected structured schema"):
        normalise_fields_with_llm(build_llm_test_request(), AppConfig(), client_factory=FakeClient)
