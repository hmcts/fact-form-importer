"""OpenAI SDK helpers for optional LLM-assisted processing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from openai import OpenAI

from fact_form_importer.config import AppConfig


@dataclass(frozen=True)
class LlmCheckResult:
    base_url: str
    model: str
    output_preview: str


def check_llm_connection(
    config: AppConfig | None = None,
    client_factory: Callable[..., Any] = OpenAI,
) -> LlmCheckResult:
    """Call the configured OpenAI-compatible endpoint with a tiny sanity prompt."""

    app_config = config or AppConfig()
    if not app_config.openai_base_url:
        raise ValueError("OPENAI_BASE_URL is required for check-llm")
    if not app_config.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for check-llm")
    if not app_config.openai_model:
        raise ValueError("OPENAI_MODEL is required for check-llm")

    client = client_factory(
        base_url=app_config.openai_base_url,
        api_key=app_config.openai_api_key,
    )
    response = client.responses.create(
        model=app_config.openai_model,
        input="Reply with exactly: OK",
    )

    return LlmCheckResult(
        base_url=app_config.openai_base_url,
        model=app_config.openai_model,
        output_preview=_response_preview(response),
    )


def _response_preview(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text).strip()

    output = getattr(response, "output", None)
    if output:
        return str(output[0]).strip()

    return str(response).strip()
