import httpx
import pytest

from fact_form_importer.validators.fact_api_courts import court_slug_exists_in_fact_api


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


def test_court_slug_exists_in_fact_api_raises_for_auth_or_server_errors():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401)))

    with pytest.raises(httpx.HTTPStatusError):
        court_slug_exists_in_fact_api(
            court_slug="fleetwood-court",
            base_url="https://fact-data-api.example.test",
            bearer_token="wrong-token",
            client=client,
        )
