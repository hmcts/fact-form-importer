"""FaCT Data API court lookup helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx


@dataclass(frozen=True)
class CourtSlugSuggestion:
    submitted_slug: str
    suggested_slug: str
    suggested_court_name: str | None
    confidence: float
    query: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "submitted_slug": self.submitted_slug,
            "suggested_slug": self.suggested_slug,
            "suggested_court_name": self.suggested_court_name,
            "confidence": round(self.confidence, 3),
            "query": self.query,
            "reason": self.reason,
        }


def court_slug_exists_in_fact_api(
    court_slug: str,
    base_url: str,
    bearer_token: str,
    timeout_seconds: float = 10.0,
    client: Optional[httpx.Client] = None,
) -> bool:
    """Return whether a court slug exists in FaCT Data API."""

    if not base_url or not base_url.strip():
        raise ValueError("FaCT Data API base URL must not be blank")
    if not bearer_token or not bearer_token.strip():
        raise ValueError("FaCT Data API bearer token must not be blank")
    if not court_slug or not court_slug.strip():
        raise ValueError("Court slug must not be blank")

    close_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds)
    headers = {"Authorization": f"Bearer {bearer_token}"}
    url = f"{base_url.rstrip('/')}/courts/slug/{quote(court_slug.strip(), safe='')}/v1"

    try:
        response = http_client.get(url, headers=headers)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True
    finally:
        if close_client:
            http_client.close()


def suggest_court_slug_in_fact_api(
    court_slug: str,
    raw_value: str | None,
    base_url: str,
    bearer_token: str,
    timeout_seconds: float = 10.0,
    client: Optional[httpx.Client] = None,
) -> CourtSlugSuggestion | None:
    """Return the strongest FaCT search suggestion for a missing court slug."""

    if not base_url or not base_url.strip():
        raise ValueError("FaCT Data API base URL must not be blank")
    if not bearer_token or not bearer_token.strip():
        raise ValueError("FaCT Data API bearer token must not be blank")
    if not court_slug or not court_slug.strip():
        raise ValueError("Court slug must not be blank")

    close_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds)
    headers = {"Authorization": f"Bearer {bearer_token}"}
    candidates: dict[str, tuple[float, str, dict[str, Any]]] = {}

    try:
        for query in _court_search_queries(court_slug, raw_value):
            url = f"{base_url.rstrip('/')}/search/courts/v1/name?{urlencode({'q': query})}"
            response = http_client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                continue

            for result in payload[:10]:
                if not isinstance(result, dict):
                    continue

                suggested_slug = _candidate_slug(result)
                if not suggested_slug:
                    continue

                confidence = _candidate_confidence(query, result)
                existing = candidates.get(suggested_slug)
                if existing is None or confidence > existing[0]:
                    candidates[suggested_slug] = (confidence, query, result)
    finally:
        if close_client:
            http_client.close()

    if not candidates:
        return None

    confidence, query, result = max(candidates.values(), key=lambda item: item[0])
    suggested_slug = _candidate_slug(result)
    if not suggested_slug:
        return None

    return CourtSlugSuggestion(
        submitted_slug=court_slug,
        suggested_slug=suggested_slug,
        suggested_court_name=_candidate_name(result),
        confidence=confidence,
        query=query,
        reason="Best match from FaCT court name search",
    )


def _court_search_queries(court_slug: str, raw_value: str | None) -> list[str]:
    queries = []
    for value in [court_slug, raw_value]:
        if not value:
            continue

        humanised = _humanise_court_identifier(str(value))
        humanised = re.sub(r"\bobjection to being a slug\b.*$", "", humanised, flags=re.IGNORECASE).strip()
        _append_query(queries, humanised)

        stripped_prefix = re.sub(r"^[a-z]{2}\s+([a-z].*)$", r"\1", humanised, flags=re.IGNORECASE)
        _append_query(queries, stripped_prefix)

        place_only = re.sub(
            (
                r"\b(county|family|crown|magistrates|court|courts|law|civil|justice|centre|"
                r"tribunal|combined|social|security|child|support|employment|appeal|england|"
                r"wales|and|of|the|chamber)\b"
            ),
            " ",
            humanised,
            flags=re.IGNORECASE,
        )
        _append_query(queries, re.sub(r"\s+", " ", place_only).strip())

    return queries[:6]


def _append_query(queries: list[str], query: str) -> None:
    if len(query) >= 3 and query not in queries:
        queries.append(query)


def _humanise_court_identifier(value: str) -> str:
    value = re.sub(r"^https?[-:/]+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"find-court-tribunal service gov uk courts", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    value = re.sub(r"[._/-]+", " ", value)
    value = re.sub(r"\b(crowncourt)\b", "crown court", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(magistratescourt)\b", "magistrates court", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(countycourt)\b", "county court", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(justicecentre)\b", "justice centre", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _candidate_confidence(query: str, result: dict[str, Any]) -> float:
    query_slug = _slugify(query)
    return max(
        SequenceMatcher(None, query_slug, _slugify(value)).ratio()
        for value in [_candidate_slug(result), _candidate_name(result)]
        if value
    )


def _candidate_slug(result: dict[str, Any]) -> str | None:
    value = result.get("slug") or result.get("court_slug")
    return str(value) if value else None


def _candidate_name(result: dict[str, Any]) -> str | None:
    value = result.get("name") or result.get("court_name")
    return str(value) if value else None


def _slugify(value: str | None) -> str:
    if not value:
        return ""

    candidate = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(value))
    candidate = candidate.lower().replace("&", " and ")
    candidate = re.sub(r"[^a-z0-9]+", "-", candidate)
    return re.sub(r"-+", "-", candidate).strip("-")
