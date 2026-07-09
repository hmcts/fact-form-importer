from types import SimpleNamespace

import pytest

from fact_form_importer.config import AppConfig
from fact_form_importer.llm.openai_client import check_llm_connection


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
