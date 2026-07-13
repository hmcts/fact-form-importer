import httpx
import pytest

from fact_form_importer.validators.fact_api_courts import (
    court_slug_exists_in_fact_api,
    lookup_court_by_slug_in_fact_api,
    suggest_court_slug_in_fact_api,
)


def test_court_slug_exists_in_fact_api_returns_true_for_200():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"slug": "fleetwood-court"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    exists = court_slug_exists_in_fact_api(
        court_slug="fleetwood-court",
        base_url="https://fact-data-api.example.test",
        bearer_token="token",
        client=client,
    )

    assert exists is True
    assert requests[0].url.path == "/courts/slug/fleetwood-court/v1"
    assert requests[0].headers["authorization"] == "Bearer token"


def test_court_slug_exists_in_fact_api_returns_false_for_404():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))

    exists = court_slug_exists_in_fact_api(
        court_slug="missing-court",
        base_url="https://fact-data-api.example.test",
        bearer_token="token",
        client=client,
    )

    assert exists is False


def test_lookup_court_by_slug_returns_fact_uuid():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"id": "court-id", "slug": "fleetwood-court", "name": "Fleetwood Court"}
            )
        )
    )

    court = lookup_court_by_slug_in_fact_api(
        court_slug="fleetwood-court",
        base_url="https://fact-data-api.example.test",
        bearer_token="token",
        client=client,
    )

    assert court.court_id == "court-id"
    assert court.name == "Fleetwood Court"


def test_court_slug_exists_in_fact_api_raises_for_auth_or_server_errors():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401)))

    with pytest.raises(httpx.HTTPStatusError):
        court_slug_exists_in_fact_api(
            court_slug="fleetwood-court",
            base_url="https://fact-data-api.example.test",
            bearer_token="wrong-token",
            client=client,
        )


def test_suggest_court_slug_in_fact_api_returns_best_match():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "slug": "shrewsbury-crown-court",
                    "name": "Shrewsbury Crown Court",
                }
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    suggestion = suggest_court_slug_in_fact_api(
        court_slug="shrewsburycrowncourt",
        raw_value="shrewsbury.crowncourt",
        base_url="https://fact-data-api.example.test",
        bearer_token="token",
        client=client,
    )

    assert suggestion is not None
    assert suggestion.suggested_slug == "shrewsbury-crown-court"
    assert suggestion.suggested_court_name == "Shrewsbury Crown Court"
    assert suggestion.confidence == 1
    assert requests[0].url.path == "/search/courts/v1/name"
    assert requests[0].headers["authorization"] == "Bearer token"


def test_suggest_court_slug_in_fact_api_returns_none_when_no_candidates():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])))

    suggestion = suggest_court_slug_in_fact_api(
        court_slug="missing-court",
        raw_value="missing court",
        base_url="https://fact-data-api.example.test",
        bearer_token="token",
        client=client,
    )

    assert suggestion is None
