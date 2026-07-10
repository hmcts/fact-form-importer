"""Select safe fields for optional LLM-assisted normalisation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from fact_form_importer.cleaners.emails import extract_email_addresses
from fact_form_importer.cleaners.phones import extract_uk_phones
from fact_form_importer.cleaners.postcodes import normalise_uk_postcode
from fact_form_importer.config import FieldRule, FieldRulesConfig
from fact_form_importer.llm.schemas import LlmField
from fact_form_importer.models.court_submission import Address, ContactDetail, CourtSubmission, OpeningHoursSet
from fact_form_importer.validators.vocabularies import Vocabularies


VOCABULARY_BY_FIELD = {
    "facilities.hearing_enhancement_equipment": "hearing_enhancement_options",
    "facilities.food_and_drink": "food_and_drink_options",
    "address.address_type": "address_types",
    "address.areas_of_law": "areas_of_law",
    "address.court_types": "court_types",
    "counter_service.assists_with": "counter_service_assistance",
    "contact.description": "contact_description_types",
    "opening_hours.type": "opening_hour_types",
}

SENSITIVE_OR_DETERMINISTIC_TOKENS = {
    "email",
    "phone",
    "postcode",
    "slug",
    "same_monday_to_friday",
    "time",
}
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)


@dataclass(frozen=True)
class _CandidateField:
    rule_name: str
    field_path: str
    value: Any


def select_llm_fields(
    submission: CourtSubmission,
    field_rules: FieldRulesConfig | dict[str, Any],
    vocabularies: Vocabularies | None = None,
) -> list[LlmField]:
    """Return the minimal safe field set eligible for LLM normalisation.

    This function only selects fields. It never sends metadata, never sends the
    full row, and never sends deterministic/sensitive field types such as
    phones, emails, postcodes, slugs, booleans or opening-hours time values.
    """

    rules = _field_rules_dict(field_rules)
    selected: list[LlmField] = []

    for candidate in _candidate_fields(submission):
        rule = rules.get(candidate.rule_name)
        if rule is None or not rule.llm.enabled:
            continue
        if _is_sensitive_or_deterministic(candidate.rule_name):
            continue
        if _is_empty(candidate.value):
            continue
        if contains_embedded_sensitive_value(candidate.value):
            continue
        if _requires_failed_vocab_match(rule, candidate.rule_name):
            vocabulary_name = VOCABULARY_BY_FIELD.get(candidate.rule_name)
            if vocabulary_name is None:
                continue
            if vocabularies is not None and _value_matches_vocab(candidate.value, vocabulary_name, vocabularies):
                continue

        selected.append(
            LlmField(
                field=candidate.field_path,
                raw_value=candidate.value,
                cleaned_value=candidate.value,
            )
        )

    return selected


def allowed_vocabularies_for_llm_fields(
    fields: Iterable[LlmField],
    vocabularies: Vocabularies | None,
) -> dict[str, list[str]]:
    """Return only vocabulary values relevant to the selected LLM fields."""

    if vocabularies is None:
        return {}

    allowed: dict[str, list[str]] = {}
    for field in fields:
        vocabulary_name = vocabulary_name_for_field_path(field.field)
        if vocabulary_name is None:
            continue
        allowed[field.field] = [entry.name for entry in vocabularies.get(vocabulary_name)]

    return allowed


def field_rules_for_llm_fields(
    fields: Iterable[LlmField],
    field_rules: FieldRulesConfig | dict[str, Any],
) -> dict[str, list[str]]:
    """Return only field-specific LLM rules relevant to the selected fields."""

    rules = _field_rules_dict(field_rules)
    selected_rules: dict[str, list[str]] = {}

    for field in fields:
        rule_name = rule_name_from_field_path(field.field)
        rule = rules.get(rule_name)
        if rule is not None and rule.llm.enabled:
            selected_rules[field.field] = list(rule.llm.rules)

    return selected_rules


def _candidate_fields(submission: CourtSubmission) -> Iterable[_CandidateField]:
    yield from _facility_candidates(submission)
    yield from _address_candidates(submission.addresses)
    yield from _counter_service_candidates(submission)
    yield from _contact_candidates(submission.contacts)
    yield from _opening_hours_candidates(submission.opening_hours)


def _facility_candidates(submission: CourtSubmission) -> Iterable[_CandidateField]:
    for field_name in [
        "accessible_toilet_description",
        "hearing_enhancement_equipment",
        "food_and_drink",
    ]:
        yield _CandidateField(
            rule_name=f"facilities.{field_name}",
            field_path=f"facilities.{field_name}",
            value=submission.facilities.get(field_name),
        )


def _address_candidates(addresses: list[Address]) -> Iterable[_CandidateField]:
    for address in addresses:
        prefix = f"addresses[{address.index}]"
        yield _CandidateField("address.address_type", f"{prefix}.address_type", address.address_type)
        yield _CandidateField("address.areas_of_law", f"{prefix}.areas_of_law", address.areas_of_law)
        yield _CandidateField("address.court_types", f"{prefix}.court_types", address.court_types)


def _counter_service_candidates(submission: CourtSubmission) -> Iterable[_CandidateField]:
    yield _CandidateField(
        rule_name="counter_service.assists_with",
        field_path="counter_service.assists_with",
        value=submission.counter_service.get("assists_with"),
    )


def _contact_candidates(contacts: list[ContactDetail]) -> Iterable[_CandidateField]:
    for contact in contacts:
        prefix = f"contacts[{contact.index}]"
        yield _CandidateField("contact.description", f"{prefix}.description", contact.description)
        yield _CandidateField("contact.explanation", f"{prefix}.explanation", contact.explanation)


def _opening_hours_candidates(opening_hours: list[OpeningHoursSet]) -> Iterable[_CandidateField]:
    for opening_hours_set in opening_hours:
        yield _CandidateField(
            rule_name="opening_hours.type",
            field_path=f"opening_hours[{opening_hours_set.index}].type",
            value=opening_hours_set.type,
        )


def _field_rules_dict(field_rules: FieldRulesConfig | dict[str, Any]) -> dict[str, FieldRule]:
    if isinstance(field_rules, FieldRulesConfig):
        return field_rules.fields

    fields = field_rules.get("fields", field_rules)
    return {
        field_name: rule if isinstance(rule, FieldRule) else FieldRule(**rule)
        for field_name, rule in fields.items()
    }


def _requires_failed_vocab_match(rule: FieldRule, rule_name: str) -> bool:
    return rule.llm.use_only_when == "not_exact_vocab_match" or rule_name in VOCABULARY_BY_FIELD


def _value_matches_vocab(value: Any, vocabulary_name: str, vocabularies: Vocabularies) -> bool:
    values = value if isinstance(value, list) else [value]
    populated_values = [candidate for candidate in values if not _is_empty(candidate)]
    if not populated_values:
        return True

    return all(
        vocabularies.exact_vocab_match(candidate, vocabulary_name) is not None
        or vocabularies.normalised_vocab_match(candidate, vocabulary_name) is not None
        for candidate in populated_values
    )


def rule_name_from_field_path(field_path: str) -> str:
    """Return the declarative rule name for a concrete model field path."""

    if field_path.startswith("addresses["):
        return "address." + field_path.split("].", 1)[1]
    if field_path.startswith("contacts["):
        return "contact." + field_path.split("].", 1)[1]
    if field_path.startswith("opening_hours["):
        return "opening_hours." + field_path.split("].", 1)[1]
    return field_path


def vocabulary_name_for_field_path(field_path: str) -> str | None:
    """Return the only vocabulary relevant to an LLM field path, if any."""

    return VOCABULARY_BY_FIELD.get(rule_name_from_field_path(field_path))


def _is_sensitive_or_deterministic(rule_name: str) -> bool:
    return any(token in rule_name for token in SENSITIVE_OR_DETERMINISTIC_TOKENS)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not any(not _is_empty(item) for item in value)
    return False


def contains_embedded_sensitive_value(value: Any) -> bool:
    """Avoid leaking contact/postcode data from otherwise public free text."""

    values = value if isinstance(value, list) else [value]
    for item in values:
        if not isinstance(item, str):
            continue
        if EMAIL_PATTERN.search(item) or extract_email_addresses(item) or extract_uk_phones(item):
            return True
        postcode = normalise_uk_postcode(item, "llm_field_selection")
        if postcode.value is not None and not postcode.issues:
            return True
    return False
