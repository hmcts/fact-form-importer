"""OpenAI-compatible client for LLM-assisted field normalisation."""

from __future__ import annotations

import json
from typing import Any, Callable

from openai import OpenAI
from pydantic import ValidationError

from fact_form_importer.config import AppConfig
from fact_form_importer.llm.prompts import SYSTEM_PROMPT, build_llm_input
from fact_form_importer.llm.schemas import (
    LlmAddressCandidate,
    LlmAddressCandidateRequest,
    LlmField,
    LlmNormalisationRequest,
    LlmNormalisationResponse,
)


class LlmResponseParseError(ValueError):
    """The model responded, but its structured response could not be parsed."""


def normalise_fields_with_llm(
    request: LlmNormalisationRequest,
    config: AppConfig | None = None,
    client_factory: Callable[..., Any] = OpenAI,
) -> LlmNormalisationResponse:
    """Normalise selected fields using the configured OpenAI-compatible endpoint."""

    app_config = config or AppConfig()
    validate_openai_config(app_config, command_name="normalise_fields_with_llm")

    client = client_factory(
        base_url=app_config.openai_base_url,
        api_key=app_config.openai_api_key,
    )
    response = client.responses.create(
        model=app_config.openai_model,
        instructions=SYSTEM_PROMPT,
        input=build_llm_input(request),
        text={
            "format": {
                "type": "json_schema",
                "name": "llm_normalisation_response",
                "schema": LlmNormalisationResponse.model_json_schema(),
                "strict": True,
            }
        },
    )

    try:
        return LlmNormalisationResponse.model_validate(_response_json(response))
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise LlmResponseParseError("LLM response did not match the expected structured schema") from exc


def validate_openai_config(config: AppConfig, command_name: str) -> None:
    """Fail before model calls when the OpenAI-compatible configuration is incomplete."""

    if not config.openai_base_url:
        raise ValueError(f"OPENAI_BASE_URL is required for {command_name}")
    if not config.openai_api_key:
        raise ValueError(f"OPENAI_API_KEY is required for {command_name}")
    if not config.openai_model:
        raise ValueError(f"OPENAI_MODEL is required for {command_name}")


def _response_json(response: Any) -> Any:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return json.loads(str(output_text))

    output = getattr(response, "output", None)
    if output:
        first = output[0]
        content = getattr(first, "content", None)
        if content:
            first_content = content[0]
            text = getattr(first_content, "text", None)
            if text:
                return json.loads(str(text))
        return json.loads(str(first))

    return json.loads(str(response))


def build_llm_test_request() -> LlmNormalisationRequest:
    return LlmNormalisationRequest(
        record_id="llm-test-record",
        source_row_number=1,
        court_slug="example-court",
        fields=[
            LlmField(
                field="facilities.accessible_toilet_description",
                raw_value="Ask security. Disabled toilet near reception.",
                cleaned_value="Ask security. Disabled toilet near reception.",
            ),
            LlmField(
                field="facilities.hearing_enhancement_equipment",
                raw_value="Loop available",
                cleaned_value="Loop available",
            ),
            LlmField(
                field="facilities.food_and_drink",
                raw_value="water; snacks machine",
                cleaned_value="water; snacks machine",
            ),
            LlmField(
                field="address.address_type",
                raw_value="visits and post",
                cleaned_value="visits and post",
            ),
            LlmField(
                field="address.areas_of_law",
                raw_value="family private law; money",
                cleaned_value="family private law; money",
            ),
            LlmField(
                field="address.court_types",
                raw_value="magistrates and county",
                cleaned_value="magistrates and county",
            ),
            LlmField(
                field="counter_service.assists_with",
                raw_value="paperwork and help at court",
                cleaned_value="paperwork and help at court",
            ),
            LlmField(
                field="contact.description",
                raw_value="general immigration appointments",
                cleaned_value="general immigration appointments",
            ),
            LlmField(
                field="contact.explanation",
                raw_value="Breathing Space team - for debt pause queries",
                cleaned_value="Breathing Space team - for debt pause queries",
            ),
            LlmField(
                field="opening_hours.type",
                raw_value="court building open",
                cleaned_value="court building open",
            ),
        ],
        allowed_vocabularies={
            "facilities.hearing_enhancement_equipment": [
                "Infrared systems and hearing loop systems are available at this court.",
                "Infrared systems are available at this court.",
                "Hearing loop systems are available at this court.",
            ],
            "facilities.food_and_drink": [
                "Free water dispensers",
                "Snack vending machines",
                "Drink vending machines",
                "A cafeteria serving hot and cold food",
            ],
            "address.address_type": [
                "Visit",
                "Send documents to",
                "Visit and send documents to",
            ],
            "address.areas_of_law": [
                "Family private law",
                "Money claims",
                "Civil",
            ],
            "address.court_types": [
                "County Court",
                "Magistrates' Court",
                "Family Court",
            ],
            "counter_service.assists_with": [
                "Forms",
                "Documents",
                "Support at court",
            ],
            "contact.description": [
                "Enquiries",
                "Appointments",
                "Immigration and Asylum Tribunal appointments",
            ],
            "opening_hours.type": [
                "Court open",
                "Telephone enquiries answered",
                "No counter service available",
            ],
        },
        field_rules={
            "facilities.accessible_toilet_description": [
                "Make the text suitable for public display.",
                "Preserve factual location information.",
                "Preserve accessible toilet information.",
                "For a simple floor location, begin with 'Available on'.",
                "'Ground floor' becomes 'Available on the ground floor.'.",
                "Use 'Available on the ground, first and third floors.' for the UCD multi-floor style.",
                "Do not invent floors, rooms, entrances, routes or availability.",
                "If the answer is vague, contradictory, or only says to ask staff/security, flag for human review.",
            ],
            "facilities.hearing_enhancement_equipment": [
                "Map the raw value to exactly one allowed hearing enhancement option.",
                "Return null if no safe match exists.",
                "Do not invent a new option.",
            ],
            "facilities.food_and_drink": [
                "Map each raw value to allowed food and drink options.",
                "Return multiple values as a semicolon-separated string.",
                "Do not invent new options.",
            ],
            "address.address_type": [
                "Map the raw value to Visit, Send documents to, or Visit and send documents to.",
                "Return null if no safe match exists.",
            ],
            "address.areas_of_law": [
                "Map each raw value to allowed areas of law.",
                "Return multiple values as a semicolon-separated string.",
                "Do not invent new areas of law.",
            ],
            "address.court_types": [
                "Map each raw value to allowed court types.",
                "Return multiple values as a semicolon-separated string.",
                "Do not invent new court types.",
            ],
            "counter_service.assists_with": [
                "Map each raw value to allowed counter service assistance options.",
                "Return multiple values as a semicolon-separated string.",
                "Do not invent new options.",
            ],
            "contact.description": [
                "Map the raw value to exactly one allowed contact description type.",
                "Return null if no safe match exists.",
                "Do not invent a new type.",
                "Flag for review if there are multiple possible meanings.",
            ],
            "contact.explanation": [
                "Make the text suitable for public display.",
                "Preserve the factual meaning.",
                "Remove days and opening/closing times represented by structured opening hours.",
                "Use operation 'clear' with value null if only a generic contact-centre label remains.",
                "For example, 'National Contact Centre for Civil and Family Court, Monday to Thursday 9am to 5pm, Friday 9am to 4:30pm' must be cleared.",
                "Do not invent extra service details.",
            ],
            "opening_hours.type": [
                "Map the raw value to exactly one allowed opening hours type.",
                "Return null if no safe match exists.",
                "Do not invent a new type.",
            ],
        },
        address_candidates=[
            LlmAddressCandidateRequest(
                address_index=1,
                submitted_address={
                    "line_1": None,
                    "line_2": "1 Justice Way",
                    "town_or_city": "Exampleton",
                    "county": None,
                },
                candidates=[
                    LlmAddressCandidate(
                        uprn="test-uprn-tribunal",
                        organisation_name="Exampleton Tribunal",
                        building_number="1",
                        thoroughfare_name="Justice Way",
                        post_town="Exampleton",
                    ),
                    LlmAddressCandidate(
                        uprn="test-uprn-offices",
                        organisation_name="Exampleton Offices",
                        building_number="1",
                        thoroughfare_name="Justice Way",
                        post_town="Exampleton",
                    ),
                ],
            )
        ],
    )
