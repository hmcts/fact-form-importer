import json

import pytest
from pydantic import ValidationError

import fact_form_importer.validators.vocabularies as vocab_module
from fact_form_importer.config import AppConfig
from fact_form_importer.validators.vocabularies import (
    Vocabularies,
    exact_vocab_match,
    load_vocabularies,
    normalised_vocab_match,
    value_in_vocab,
)


def test_load_vocabularies_from_example_config():
    vocabularies = load_vocabularies(AppConfig().vocabularies_path)

    assert vocabularies.version == "2026-07-07.1"
    assert "opening_hour_types" in vocabularies.vocabularies
    assert "contact_description_types" in vocabularies.vocabularies
    assert "address_types" in vocabularies.vocabularies
    assert "court_types" in vocabularies.vocabularies
    assert "areas_of_law" in vocabularies.vocabularies


def test_exact_vocab_match_returns_entry_for_code_name_or_alias():
    load_vocabularies(AppConfig().vocabularies_path)

    by_code = exact_vocab_match("court_open", "opening_hour_types")
    by_name = exact_vocab_match("Court open", "opening_hour_types")
    by_alias = exact_vocab_match("Magistrates Court", "court_types")

    assert by_code is not None
    assert by_code.name == "Court open"
    assert by_name is not None
    assert by_name.code == "court_open"
    assert by_alias is not None
    assert by_alias.code == "magistrates_court"


def test_normalised_vocab_match_is_case_and_space_insensitive():
    load_vocabularies(AppConfig().vocabularies_path)

    match = normalised_vocab_match("  family COURT enquiries  ", "contact_description_types")

    assert match is not None
    assert match.code == "family_court_enquiries"


def test_vocab_matches_return_none_for_empty_or_missing_values():
    vocabularies = load_vocabularies(AppConfig().vocabularies_path)

    assert vocabularies.exact_vocab_match(None, "court_types") is None
    assert vocabularies.exact_vocab_match("   ", "court_types") is None
    assert vocabularies.exact_vocab_match("Missing", "court_types") is None
    assert vocabularies.normalised_vocab_match(None, "court_types") is None
    assert vocabularies.normalised_vocab_match("   ", "court_types") is None
    assert vocabularies.normalised_vocab_match("Missing", "court_types") is None


def test_value_in_vocab_accepts_code_name_or_alias():
    load_vocabularies(AppConfig().vocabularies_path)

    assert value_in_vocab("county_court", "court_types") is True
    assert value_in_vocab("County Court", "court_types") is True
    assert value_in_vocab("Magistrates Court", "court_types") is True
    assert value_in_vocab("Not a court type", "court_types") is False
    assert value_in_vocab("County Court", "missing_vocab") is False


def test_loaded_vocabularies_object_can_be_used_without_module_global(tmp_path):
    path = tmp_path / "vocabularies.json"
    path.write_text(
        json.dumps(
            {
                "version": "test.1",
                "opening_hour_types": [
                    {
                        "code": "counter_open",
                        "name": "Counter open",
                        "aliases": ["Counter service open"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    vocabularies = load_vocabularies(path)

    assert vocabularies.exact_vocab_match("Counter open", "opening_hour_types").code == (
        "counter_open"
    )
    assert vocabularies.normalised_vocab_match(
        "counter SERVICE open", "opening_hour_types"
    ).code == "counter_open"
    assert vocabularies.value_in_vocab("counter_open", "opening_hour_types") is True


def test_vocabularies_support_wrapped_shape_for_future_sources():
    vocabularies = Vocabularies(
        version="test.2",
        vocabularies={
            "address_types": [
                {"code": "visit", "name": "Visit"},
            ]
        },
    )

    assert vocabularies.value_in_vocab("visit", "address_types") is True


def test_default_vocabularies_loads_when_module_global_is_empty():
    vocab_module._DEFAULT_VOCABULARIES = None

    match = exact_vocab_match("Court open", "opening_hour_types")

    assert match is not None
    assert match.code == "court_open"


def test_vocabularies_reject_empty_or_blank_values():
    with pytest.raises(ValidationError):
        Vocabularies.model_validate(["not", "a", "dict"])

    with pytest.raises(ValidationError, match="At least one vocabulary"):
        Vocabularies(version="test.1", vocabularies={})

    with pytest.raises(ValidationError, match="At least one vocabulary"):
        Vocabularies(version="test.1")

    with pytest.raises(ValidationError, match="Vocabulary names must not be blank"):
        Vocabularies(
            version="test.1",
            vocabularies={
                " ": [
                    {"code": "visit", "name": "Visit"},
                ]
            },
        )

    with pytest.raises(ValidationError, match="must contain at least one entry"):
        Vocabularies(
            version="test.1",
            vocabularies={
                "address_types": [],
            },
        )

    with pytest.raises(ValidationError, match="must not be blank"):
        Vocabularies(
            version="test.1",
            vocabularies={
                "address_types": [
                    {"code": "", "name": "Visit"},
                ]
            },
        )

    with pytest.raises(ValidationError, match="aliases must not be blank"):
        Vocabularies(
            version="test.1",
            vocabularies={
                "address_types": [
                    {"code": "visit", "name": "Visit", "aliases": [""]},
                ]
            },
        )
