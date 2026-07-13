"""Small HTTP client for the existing FaCT endpoints used by the action report."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from fact_form_importer.config import AppConfig
from fact_form_importer.validators.fact_api_courts import CourtReference


@dataclass(frozen=True)
class ApiResponse:
    status_code: int
    body: Any = None


class FactApiExecutionClient:
    def __init__(self, config: AppConfig, client: httpx.Client | None = None) -> None:
        if not config.fact_data_api_base_url or not config.fact_data_api_bearer_token:
            raise ValueError("FACT_DATA_API_BASE_URL and FACT_DATA_API_BEARER_TOKEN are required")
        self.base_url = config.fact_data_api_base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {config.fact_data_api_bearer_token}"}
        self.user_id = config.fact_data_api_user_id
        self._client = client or httpx.Client(timeout=15.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def lookup_court(self, court_slug: str) -> CourtReference | None:
        response = self._client.get(
            f"{self.base_url}/courts/slug/{quote(court_slug, safe='')}/v1", headers=self.headers
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("FaCT court lookup response must contain an id")
        return CourtReference(
            court_id=str(payload["id"]),
            slug=str(payload.get("slug") or court_slug),
            name=str(payload["name"]) if payload.get("name") else None,
        )

    def get(self, path: str) -> ApiResponse:
        response = self._client.get(f"{self.base_url}{path}", headers=self.headers)
        return ApiResponse(status_code=response.status_code, body=_response_body(response))

    def write(self, method: str, path: str, body: dict[str, Any]) -> ApiResponse:
        if not self.user_id:
            raise ValueError(
                "FACT_DATA_API_USER_ID is required for audited FaCT API write requests"
            )
        try:
            UUID(self.user_id)
        except ValueError as exc:
            raise ValueError("FACT_DATA_API_USER_ID must be a valid UUID") from exc

        response = self._client.request(
            method,
            f"{self.base_url}{path}",
            headers={**self.headers, "X-User-Id": self.user_id},
            json=body,
        )
        return ApiResponse(status_code=response.status_code, body=_response_body(response))


def _response_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text
