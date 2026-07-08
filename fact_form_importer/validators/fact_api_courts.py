"""FaCT Data API court lookup helpers."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

import httpx


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
