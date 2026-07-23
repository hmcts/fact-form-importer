import pytest
import builtins

from fact_form_importer.cleaners.booleans import normalise_yes_no
from fact_form_importer.cleaners.emails import extract_email_addresses, normalise_email
from fact_form_importer.cleaners.multiselect import split_multiselect
from fact_form_importer.cleaners.phones import (
    _fallback_format_phone,
    _looks_like_uk_phone,
    extract_uk_phones,
    normalise_uk_phone,
)
from fact_form_importer.cleaners.postcodes import normalise_uk_postcode
from fact_form_importer.cleaners.slug import normalise_court_slug
from fact_form_importer.cleaners.strings import collapse_spaces, null_if_empty_like, trim
from fact_form_importer.cleaners.times import parse_time_cell, parse_time_parts


@pytest.mark.parametrize("value", ["", " ", "N/A", "n/a", "NA", "-", ".", None])
def test_null_if_empty_like(value):
    assert null_if_empty_like(value) is None


def test_trim_and_collapse_spaces():
    assert trim("  hello  ") == "hello"
    assert collapse_spaces(" hello \n  world ") == "hello world"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.find-court-tribunal.service.gov.uk/courts/fleetwood-court", "fleetwood-court"),
        ("ttps://www.find-court-tribunal.service.gov.uk/courts/Fleetwood Court", "fleetwood-court"),
        ("https://StAlbansCrownCourt", "st-albans-crown-court"),
        (
            "https://www.find-court-tribunal.service.gov.uk/courts/ HavantJusticeCentre",
            "havant-justice-centre",
        ),
        ("courts/FLEETWOOD-COURT", "fleetwood-court"),
        ("Fleetwood Court!!!", "fleetwood-court"),
        ("fleetwood--court", "fleetwood-court"),
    ],
)
def test_normalise_court_slug(value, expected):
    assert normalise_court_slug(value) == expected


def test_normalise_email():
    assert normalise_email(" USER@Example.COM ").value == "user@example.com"
    assert normalise_email("Email admin@example.com for help").value == "admin@example.com"
    assert normalise_email("\u200blondonfamily@supportthroughcourt.org").value == (
        "londonfamily@supportthroughcourt.org"
    )

    result = normalise_email("not an email")

    assert result.value == "not an email"
    assert result.issues[0].code == "INVALID_EMAIL"


def test_normalise_email_fallback_when_email_validator_is_unavailable(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "email_validator":
            raise ModuleNotFoundError("email_validator")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert normalise_email("USER@Example.COM").value == "user@example.com"
    assert normalise_email("Email court@example.com for help").value == "court@example.com"

    result = normalise_email("not an email")

    assert result.value == "not an email"
    assert result.issues[0].code == "INVALID_EMAIL"


def test_extract_email_addresses():
    assert extract_email_addresses("Email A@Example.COM or b@example.com.") == [
        "a@example.com",
        "b@example.com",
    ]


def test_normalise_uk_phone():
    assert normalise_uk_phone("02079460000").value == "020 7946 0000"
    assert normalise_uk_phone("Call 0208 603 0440 or email test@example.com").value == (
        "020 8603 0440"
    )

    result = normalise_uk_phone("not a phone")

    assert result.value == "not a phone"
    assert result.issues[0].code == "INVALID_PHONE"


def test_normalise_uk_phone_fallback_when_phonenumbers_is_unavailable(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "phonenumbers":
            raise ModuleNotFoundError("phonenumbers")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert normalise_uk_phone("+44 20 7946 0000").value == "020 7946 0000"
    assert extract_uk_phones("02079460000") == ["020 7946 0000"]
    assert extract_uk_phones("not a phone") == []

    result = normalise_uk_phone("not a phone")

    assert result.value == "not a phone"
    assert result.issues[0].code == "INVALID_PHONE"


def test_extract_uk_phones():
    assert extract_uk_phones("Call 0207 073 4112 and 0207 073 4157") == [
        "020 7073 4112",
        "020 7073 4157",
    ]


def test_phone_fallback_helpers():
    assert _looks_like_uk_phone("+44 20 7946 0000") is True
    assert _looks_like_uk_phone("123") is False
    assert _fallback_format_phone("+44 20 7946 0000") == "020 7946 0000"
    assert _fallback_format_phone("01234 567890") == "01234 567890"
    assert _fallback_format_phone("ext 123") == "ext 123"


def test_normalise_uk_postcode():
    assert normalise_uk_postcode(None).value is None
    assert normalise_uk_postcode("sw1a1aa").value == "SW1A 1AA"
    assert normalise_uk_postcode("GIR0AA").value == "GIR 0AA"
    assert normalise_uk_postcode("YO1 9WZ").value == "YO1 9WZ"
    assert normalise_uk_postcode("PO9 2AL").value == "PO9 2AL"
    assert normalise_uk_postcode("CO2 7EF").value == "CO2 7EF"
    repaired = normalise_uk_postcode("CRO 2RF")
    assert repaired.value == "CR0 2RF"
    assert repaired.issues[0].code == "POSTCODE_TYPO_REPAIRED"

    invalid_character = normalise_uk_postcode("CF10 £PG")
    assert invalid_character.value == "CF10 £PG"
    assert invalid_character.issues[0].code == "INVALID_POSTCODE"

    result = normalise_uk_postcode("not a postcode")

    assert result.value == "NOT A POSTCODE"
    assert result.issues[0].code == "INVALID_POSTCODE"


def test_normalise_yes_no():
    assert normalise_yes_no("Yes").value is True
    assert normalise_yes_no("n").value is False

    result = normalise_yes_no("maybe")

    assert result.value is None
    assert result.issues[0].code == "UNKNOWN_BOOLEAN"


def test_parse_time_parts():
    result = parse_time_parts("9", "05")

    assert result.value == "09:05"
    assert result.status == "valid_time"


@pytest.mark.parametrize(
    ("hour_value", "minute_value", "expected_value"),
    [
        ("09:00", "09:00", "09:00"),
        ("17:00", "17:00", "17:00"),
        ("17:00", "17;00", "17:00"),
        ("09;30", "30", "09:30"),
        ("09:30", "30", "09:30"),
        ("09:00", None, "09:00"),
    ],
)
def test_parse_time_parts_accepts_full_time_in_hour_field(
    hour_value, minute_value, expected_value
):
    result = parse_time_parts(hour_value, minute_value)

    assert result.value == expected_value
    assert result.status == "valid_time"


@pytest.mark.parametrize(
    ("hour_value", "minute_value", "expected_value"),
    [
        ("08", ".30", "08:30"),
        ("09", ".00", "09:00"),
        ("15", ":00", "15:00"),
        ("09;00", "00:00", "09:00"),
        ("10.00 AM", "00:00", "10:00"),
        ("14:00PM", "00:00", "14:00"),
    ],
)
def test_parse_time_parts_repairs_safe_punctuation_and_redundant_zero_minutes(
    hour_value, minute_value, expected_value
):
    result = parse_time_parts(hour_value, minute_value)

    assert result.value == expected_value
    assert result.status == "valid_time"


def test_parse_time_parts_rejects_full_time_with_conflicting_minute():
    result = parse_time_parts("09:00", "30")

    assert result.value is None
    assert result.status == "invalid"
    assert result.issues[0].code == "INVALID_TIME"


def test_parse_time_parts_empty_partial_and_invalid():
    assert parse_time_parts(None, None).status == "empty"

    partial = parse_time_parts("9", None)
    assert partial.status == "invalid"
    assert partial.issues[0].code == "INVALID_TIME"

    non_numeric = parse_time_parts("nine", "00")
    assert non_numeric.status == "invalid"
    assert non_numeric.issues[0].code == "INVALID_TIME"


@pytest.mark.parametrize(
    "status",
    ["Appointment Only", "appointments only", "Counter service not available"],
)
def test_parse_time_parts_classifies_repeated_known_statuses(status):
    result = parse_time_parts(status, status)

    assert result.value is None
    assert result.status == "known_text_status"


@pytest.mark.parametrize(
    ("value", "expected_value", "expected_status"),
    [
        ("09", "09:00", "valid_time"),
        ("09:00", "09:00", "valid_time"),
        ("17;00", "17:00", "valid_time"),
        ("09:00 am", "09:00", "valid_time"),
        ("00", "00:00", "valid_time"),
        ("N/A", None, "empty"),
        (".", None, "empty"),
        ("appointment only", None, "known_text_status"),
        ("No Counter Service", None, "known_text_status"),
    ],
)
def test_parse_time_cell(value, expected_value, expected_status):
    result = parse_time_cell(value)

    assert result.value == expected_value
    assert result.status == expected_status


def test_parse_time_cell_invalid():
    result = parse_time_cell("25:99")

    assert result.value is None
    assert result.status == "invalid"
    assert result.issues[0].code == "INVALID_TIME"


def test_parse_time_cell_handles_meridiem_and_text_failures():
    assert parse_time_cell("12:00 am").value == "00:00"
    assert parse_time_cell("12:00 pm").value == "12:00"
    assert parse_time_cell("1:05 pm").value == "13:05"

    result = parse_time_cell("ten past nine")

    assert result.value is None
    assert result.status == "invalid"
    assert result.issues[0].code == "INVALID_TIME"


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        ("09:00 am", "240 minutes", "09:00"),
        ("16:30 pm", "60 minutes", "16:30"),
        ("09:00", "?", "09:00"),
        ("17:00", "?", "17:00"),
    ],
)
def test_parse_time_parts_recovers_one_unambiguous_time_with_known_noise(
    hour, minute, expected
):
    result = parse_time_parts(hour, minute)

    assert result.value == expected
    assert result.status == "valid_time"
    assert result.issues[0].code == "TIME_FORMAT_RECOVERED"
    assert result.issues[0].raw_value == f"{hour}:{minute}"
    assert result.issues[0].cleaned_value == expected


@pytest.mark.parametrize("value", ["09:00:?", "09:00:240 minutes"])
def test_parse_time_cell_recovers_safe_trailing_noise(value):
    result = parse_time_cell(value)

    assert result.value == "09:00"
    assert result.issues[0].code == "TIME_FORMAT_RECOVERED"


@pytest.mark.parametrize("value", ["09-4.30:09-4.30", "morning 09:00 afternoon"])
def test_parse_time_cell_does_not_guess_ambiguous_values(value):
    assert parse_time_cell(value).status == "invalid"


def test_split_multiselect():
    assert split_multiselect(None) == []
    assert split_multiselect("One; Two ; N/A; Three") == ["One", "Two", "Three"]
    assert split_multiselect("One\nTwo") == ["One", "Two"]
    assert split_multiselect("N/A") == []
