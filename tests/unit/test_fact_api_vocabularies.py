import httpx
import pytest

from fact_form_importer.validators.fact_api_vocabularies import load_vocabularies_from_fact_api
from fact_form_importer.validators.vocabularies import Vocabularies


def test_load_vocabularies_from_fact_api_fetches_type_endpoints_and_merges_fallback():
    requested_paths = []
    auth_headers = []

    def handler(request):
        requested_paths.append(request.url.path)
        auth_headers.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json=[
                {"id": "id-1", "name": "Example Type"},
            ],
        )

    fallback = Vocabularies(
        version="local",
        vocabularies={
            "food_and_drink_options": [
                {"code": "water", "name": "Free water dispensers"},
            ]
        },
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    vocabularies = load_vocabularies_from_fact_api(
        base_url="https://fact-data-api.example.test",
        bearer_token="token",
        fallback=fallback,
        client=client,
    )

    assert requested_paths == [
        "/types/v1/areas-of-law",
        "/types/v1/court-types",
        "/types/v1/opening-hours-types",
        "/types/v1/contact-description-types",
    ]
    assert set(auth_headers) == {"Bearer token"}
    assert vocabularies.version == "fact-data-api"
    assert vocabularies.value_in_vocab("Example Type", "contact_description_types") is True
    assert vocabularies.normalised_vocab_match("Example Type", "contact_description_types").api_id == "id-1"
    assert vocabularies.value_in_vocab("Free water dispensers", "food_and_drink_options") is True


def test_load_vocabularies_from_fact_api_rejects_unexpected_response_shape():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))

    with pytest.raises(ValueError, match="must be a list"):
        load_vocabularies_from_fact_api(
            base_url="https://fact-data-api.example.test",
            fallback=Vocabularies(
                version="local",
                vocabularies={"food_and_drink_options": [{"code": "water", "name": "Water"}]},
            ),
            client=client,
        )


def test_load_vocabularies_from_fact_api_requires_names():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[{"id": "id-1"}]))
    )

    with pytest.raises(ValueError, match="must contain a name"):
        load_vocabularies_from_fact_api(
            base_url="https://fact-data-api.example.test",
            fallback=Vocabularies(
                version="local",
                vocabularies={"food_and_drink_options": [{"code": "water", "name": "Water"}]},
            ),
            client=client,
        )
