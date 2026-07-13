"""Load controlled vocabularies from FaCT Data API type endpoints."""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from fact_form_importer.validators.vocabularies import Vocabularies, VocabularyEntry

FACT_API_VOCABULARY_ENDPOINTS = {
    "areas_of_law": "/types/v1/areas-of-law",
    "court_types": "/types/v1/court-types",
    "opening_hour_types": "/types/v1/opening-hours-types",
    "contact_description_types": "/types/v1/contact-description-types",
}


def load_vocabularies_from_fact_api(
    base_url: str,
    bearer_token: Optional[str] = None,
    fallback: Optional[Vocabularies] = None,
    timeout_seconds: float = 10.0,
    client: Optional[httpx.Client] = None,
) -> Vocabularies:
    """Load FaCT API-owned vocabularies and merge them with local fallback lists."""

    if not base_url.strip():
        raise ValueError("FaCT Data API base URL must not be blank")

    vocabularies = dict(fallback.vocabularies) if fallback else {}
    close_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds)

    try:
        for vocabulary_name, endpoint in FACT_API_VOCABULARY_ENDPOINTS.items():
            response = http_client.get(
                _url(base_url, endpoint),
                headers=_headers(bearer_token),
            )
            response.raise_for_status()
            vocabularies[vocabulary_name] = _entries_from_api_items(response.json())
    finally:
        if close_client:
            http_client.close()

    return Vocabularies(version="fact-data-api", vocabularies=vocabularies)


def _entries_from_api_items(items: Any) -> list[VocabularyEntry]:
    if not isinstance(items, list):
        raise ValueError("FaCT Data API vocabulary response must be a list")

    entries = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            raise ValueError("FaCT Data API vocabulary entries must contain a name")
        entries.append(
            VocabularyEntry(
                code=_code_for_name(str(item["name"])),
                name=str(item["name"]),
                api_id=str(item["id"]) if item.get("id") else None,
            )
        )

    return entries


def _code_for_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _headers(bearer_token: Optional[str]) -> dict[str, str]:
    if not bearer_token:
        return {}

    return {"Authorization": f"Bearer {bearer_token}"}


def _url(base_url: str, endpoint: str) -> str:
    return base_url.rstrip("/") + endpoint
