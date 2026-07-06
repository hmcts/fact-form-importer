import pytest

from fact_form_importer.cleaners.booleans import normalise_yes_no
from fact_form_importer.cleaners.emails import normalise_email
from fact_form_importer.cleaners.multiselect import split_multiselect
from fact_form_importer.cleaners.phones import normalise_uk_phone
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
        ("courts/FLEETWOOD-COURT", "fleetwood-court"),
        ("Fleetwood Court!!!", "fleetwood-court"),
        ("fleetwood--court", "fleetwood-court"),
    ],
)
def test_normalise_court_slug(value, expected):
    assert normalise_court_slug(value) == expected


def test_normalise_email():
    assert normalise_email(" USER@Example.COM ").value == "user@example.com"

    result = normalise_email("not an email")

    assert result.value == "not an email"
    assert result.issues[0].code == "INVALID_EMAIL"


def test_normalise_uk_phone():
    assert normalise_uk_phone("02079460000").value == "020 7946 0000"

    result = normalise_uk_phone("not a phone")

    assert result.value == "not a phone"
    assert result.issues[0].code == "INVALID_PHONE"


def test_normalise_uk_postcode():
    assert normalise_uk_postcode("sw1a1aa").value == "SW1A 1AA"

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
    ("value", "expected_value", "expected_status"),
    [
        ("09", "09:00", "valid_time"),
        ("09:00", "09:00", "valid_time"),
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


def test_split_multiselect():
    assert split_multiselect("One; Two ; N/A; Three") == ["One", "Two", "Three"]
    assert split_multiselect("One\nTwo") == ["One", "Two"]
    assert split_multiselect("N/A") == []
