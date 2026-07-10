from fact_form_importer.config import FieldRulesConfig
from fact_form_importer.llm.normalise import (
    allowed_vocabularies_for_llm_fields,
    field_rules_for_llm_fields,
    select_llm_fields,
)
from fact_form_importer.models.court_submission import (
    Address,
    ContactDetail,
    CourtSubmission,
    OpeningHoursSet,
)
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.validators.vocabularies import Vocabularies


def test_select_llm_fields_excludes_deterministic_and_sensitive_fields():
    submission = _submission(
        facilities={
            "accessible_parking_phone": "020 7946 0000",
            "accessible_toilet_description": "Available near reception.",
        },
        translation_email="redacted email value",
        addresses=[Address(index=1, postcode="SW1A 1AA", line_1="1 Example Street")],
        opening_hours=[OpeningHoursSet(index=1, same_monday_to_friday=True)],
    )
    rules = _field_rules(
        {
            "facilities.accessible_parking_phone": {"enabled": True},
            "facilities.accessible_toilet_description": {"enabled": True},
            "translation_email": {"enabled": True},
            "address.postcode": {"enabled": True},
            "address.line_1": {"enabled": False},
            "opening_hours.same_monday_to_friday": {"enabled": True},
        }
    )

    fields = select_llm_fields(submission, rules, _vocabularies())

    assert [field.field for field in fields] == ["facilities.accessible_toilet_description"]
    assert fields[0].raw_value == "Available near reception."


def test_select_llm_fields_skips_exact_and_normalised_vocab_matches():
    submission = _submission(
        contacts=[ContactDetail(index=1, description="enquiries")],
        opening_hours=[OpeningHoursSet(index=1, type=" Court OPEN ")],
    )
    rules = _field_rules(
        {
            "contact.description": {
                "enabled": True,
                "use_only_when": "not_exact_vocab_match",
            },
            "opening_hours.type": {
                "enabled": True,
                "use_only_when": "not_exact_vocab_match",
            },
        }
    )

    fields = select_llm_fields(submission, rules, _vocabularies())

    assert fields == []


def test_select_llm_fields_selects_ambiguous_vocab_values():
    submission = _submission(
        contacts=[
            ContactDetail(
                index=1,
                description="general immigration appointments",
                explanation="Breathing Space team - debt pause queries",
            )
        ],
        opening_hours=[OpeningHoursSet(index=1, type="court building open")],
    )
    rules = _field_rules(
        {
            "contact.description": {
                "enabled": True,
                "use_only_when": "not_exact_vocab_match",
            },
            "contact.explanation": {"enabled": True},
            "opening_hours.type": {
                "enabled": True,
                "use_only_when": "not_exact_vocab_match",
            },
        }
    )

    fields = select_llm_fields(submission, rules, _vocabularies())

    assert [field.field for field in fields] == [
        "contacts[1].description",
        "contacts[1].explanation",
        "opening_hours[1].type",
    ]


def test_selected_llm_fields_only_include_relevant_vocabularies_and_rules():
    submission = _submission(
        contacts=[ContactDetail(index=1, description="general immigration appointments")],
        opening_hours=[OpeningHoursSet(index=1, type="court building open")],
    )
    rules = _field_rules(
        {
            "contact.description": {
                "enabled": True,
                "use_only_when": "not_exact_vocab_match",
                "rules": ["Map to one contact type."],
            },
            "opening_hours.type": {
                "enabled": True,
                "use_only_when": "not_exact_vocab_match",
                "rules": ["Map to one opening hours type."],
            },
        }
    )
    vocabularies = _vocabularies()
    fields = select_llm_fields(submission, rules, vocabularies)

    allowed = allowed_vocabularies_for_llm_fields(fields, vocabularies)
    selected_rules = field_rules_for_llm_fields(fields, rules)

    assert allowed == {
        "contacts[1].description": ["Enquiries", "Appointments"],
        "opening_hours[1].type": ["Court open"],
    }
    assert selected_rules == {
        "contacts[1].description": ["Map to one contact type."],
        "opening_hours[1].type": ["Map to one opening hours type."],
    }


def _submission(**kwargs):
    defaults = {
        "source": SourceMetadata(source_row_number=2),
        "court_slug_raw": "example-court",
        "court_slug": "example-court",
    }
    defaults.update(kwargs)
    return CourtSubmission(**defaults)


def _field_rules(llm_rules_by_field):
    fields = {}
    for field_name, llm_rule in llm_rules_by_field.items():
        enabled = llm_rule.get("enabled", False)
        fields[field_name] = {
            "required": False,
            "cleaners": [],
            "validators": [],
            "llm": {
                "enabled": enabled,
                "purpose": "test" if enabled else None,
                "use_only_when": llm_rule.get("use_only_when"),
                "rules": llm_rule.get("rules", ["Test rule."]) if enabled else [],
            },
        }
    return FieldRulesConfig(version="test.1", fields=fields)


def _vocabularies():
    return Vocabularies(
        version="test.1",
        vocabularies={
            "contact_description_types": [
                {"code": "enquiries", "name": "Enquiries"},
                {"code": "appointments", "name": "Appointments"},
            ],
            "opening_hour_types": [
                {"code": "court_open", "name": "Court open"},
            ],
        },
    )
