import json

import pytest
from pydantic import ValidationError

from fact_form_importer.config import AppConfig, FieldRulesConfig, load_default_field_rules, load_field_rules


def test_load_field_rules_config_contains_required_fields():
    rules = load_default_field_rules()

    assert rules.version == "2026-07-07.1"
    assert "court_slug" in rules.fields
    assert "translation_email" in rules.fields
    assert "translation_phone" in rules.fields
    assert "facilities.accessible_toilet_description" in rules.fields
    assert "contact.description" in rules.fields
    assert "opening_hours.type" in rules.fields

    court_slug = rules.fields["court_slug"]
    assert court_slug.required is True
    assert "normalise_court_slug" in court_slug.cleaners
    assert "required" in court_slug.validators
    assert court_slug.llm.enabled is False


def test_field_rules_capture_llm_enabled_fields_declaratively():
    rules = load_default_field_rules()

    accessible_toilet = rules.fields["facilities.accessible_toilet_description"]
    contact_description = rules.fields["contact.description"]
    opening_hours_type = rules.fields["opening_hours.type"]

    assert accessible_toilet.llm.enabled is True
    assert accessible_toilet.llm.purpose == "public_text_normalisation"
    assert any("Do not invent" in rule for rule in accessible_toilet.llm.rules)

    assert contact_description.llm.enabled is True
    assert contact_description.llm.use_only_when == "not_exact_vocab_match"

    assert opening_hours_type.llm.enabled is True
    assert opening_hours_type.llm.purpose == "map_to_opening_hours_type"


def test_field_rules_include_repeated_group_rules():
    rules = load_default_field_rules()

    assert rules.fields["address.postcode"].cleaners == ["normalise_uk_postcode"]
    assert "valid_uk_postcode" in rules.fields["address.postcode"].validators

    assert rules.fields["contact.phone"].validators == [
        "valid_uk_phone_or_null",
        "phone_or_email_required",
    ]
    assert rules.fields["contact.email"].validators == [
        "valid_email_or_null",
        "phone_or_email_required",
    ]

    assert rules.fields["opening_hours.time"].cleaners == ["parse_time_parts", "parse_time_cell"]


def test_load_field_rules_from_custom_config_dir(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    rules_path = config_dir / "field_rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "version": "test.1",
                "fields": {
                    "court_slug": {
                        "required": True,
                        "cleaners": ["normalise_court_slug"],
                        "validators": ["required"],
                        "llm": {"enabled": False},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    rules = load_default_field_rules(AppConfig(config_dir=config_dir))

    assert rules.version == "test.1"
    assert rules.fields["court_slug"].required is True


def test_field_rules_reject_empty_fields():
    with pytest.raises(ValidationError, match="at least one field"):
        FieldRulesConfig(version="test.1", fields={})


def test_field_rules_reject_llm_enabled_without_purpose_or_rules():
    with pytest.raises(ValidationError, match="purpose"):
        FieldRulesConfig(
            version="test.1",
            fields={
                "field": {
                    "required": False,
                    "cleaners": [],
                    "validators": [],
                    "llm": {"enabled": True, "rules": ["Do something"]},
                }
            },
        )

    with pytest.raises(ValidationError, match="at least one rule"):
        FieldRulesConfig(
            version="test.1",
            fields={
                "field": {
                    "required": False,
                    "cleaners": [],
                    "validators": [],
                    "llm": {"enabled": True, "purpose": "test"},
                }
            },
        )


def test_field_rules_reject_blank_cleaner_or_validator_names():
    with pytest.raises(ValidationError, match="must not be blank"):
        FieldRulesConfig(
            version="test.1",
            fields={
                "field": {
                    "required": False,
                    "cleaners": ["trim", ""],
                    "validators": [],
                    "llm": {"enabled": False},
                }
            },
        )


def test_load_field_rules_reads_json_file(tmp_path):
    path = tmp_path / "field_rules.json"
    path.write_text(
        json.dumps(
            {
                "version": "test.2",
                "fields": {
                    "translation_email": {
                        "required": False,
                        "cleaners": ["normalise_email"],
                        "validators": ["valid_email_or_null"],
                        "llm": {"enabled": False},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    rules = load_field_rules(path)

    assert rules.version == "test.2"
    assert rules.fields["translation_email"].cleaners == ["normalise_email"]


def test_app_config_reads_fact_data_api_environment(monkeypatch):
    monkeypatch.setenv("FACT_DATA_API_BASE_URL", "https://fact-data-api.example.test")
    monkeypatch.setenv("FACT_DATA_API_BEARER_TOKEN", "token")

    config = AppConfig()

    assert config.fact_data_api_base_url == "https://fact-data-api.example.test"
    assert config.fact_data_api_bearer_token == "token"


def test_app_config_reads_openai_environment(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ai-foundry.example.test/openai/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "token")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.5")

    config = AppConfig()

    assert config.llm_enabled is True
    assert config.openai_base_url == "https://ai-foundry.example.test/openai/v1"
    assert config.openai_api_key == "token"
    assert config.openai_model == "gpt-5.5"


def test_app_config_defaults_llm_to_disabled(monkeypatch):
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    config = AppConfig()

    assert config.llm_enabled is False
    assert config.openai_base_url is None
    assert config.openai_api_key is None
    assert config.openai_model is None
