from dataclasses import dataclass

import pytest

from fact_form_importer.models.court_submission import Address, CourtSubmission
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.validators.os_addresses import (
    ADDRESS_OS_NORMALISED,
    ADDRESS_OS_REVIEW_REQUIRED,
    RateLimitedPostcodeLookup,
    verify_submission_addresses,
)


@dataclass
class _Response:
    status_code: int
    body: object


def test_verification_auto_normalises_only_a_unique_high_confidence_os_match():
    submission = _submission(
        Address(
            index=1,
            address_type="Visit",
            line_1="Court House, 1 Main Street",
            town_or_city="London",
            county="Greater London",
            postcode="SW1A 1AA",
        )
    )

    batch = verify_submission_addresses([submission], lambda _: _Response(200, _one_candidate()))

    address = submission.addresses[0]
    verification = batch.verifications[0]
    assert verification.status == "auto_normalised"
    assert address.line_1 == "Court House"
    assert address.line_2 == "1 Main Street"
    assert address.town_or_city == "London"
    assert address.county == "Greater London"
    assert batch.action_reason_for(submission, 1) is None
    assert any(issue.code == ADDRESS_OS_NORMALISED for issue in submission.issues)
    assert submission.raw == {"AA": "Court House, 1 Main Street"}
    assert submission.cleaned["addresses"][0]["line_1"] == "Court House"


def test_verification_keeps_ambiguous_candidates_for_review_without_mutation():
    address = Address(
        index=1,
        address_type="Visit",
        line_1="1 Main Street",
        town_or_city="London",
        postcode="SW1A 1AA",
    )
    submission = _submission(address)
    response = {
        "results": [
            _dpa("uprn-1", building_number="1", thoroughfare_name="Main Street"),
            _dpa("uprn-2", building_number="1", thoroughfare_name="Main Street"),
        ]
    }

    batch = verify_submission_addresses([submission], lambda _: _Response(200, response))

    verification = batch.verifications[0]
    assert verification.status == "review_required"
    assert verification.selected_candidate is None
    assert submission.addresses[0].line_2 is None
    assert "requires review" in batch.action_reason_for(submission, 1)
    assert any(issue.code == ADDRESS_OS_REVIEW_REQUIRED for issue in submission.issues)
    requests = batch.llm_candidates_for(submission)
    assert requests[0]["address_index"] == 1
    assert "postcode" not in requests[0]["submitted_address"]
    assert {candidate["uprn"] for candidate in requests[0]["candidates"]} == {"uprn-1", "uprn-2"}


def test_verification_caches_duplicate_postcodes_and_rate_limits_unique_requests():
    first = _submission(
        Address(index=1, line_1="Court House", town_or_city="London", postcode="SW1A 1AA")
    )
    second = CourtSubmission(
        source=SourceMetadata(source_row_number=3),
        court_slug="second-court",
        addresses=[Address(index=1, line_1="Court House", town_or_city="London", postcode="SW1A 1AA")],
    )
    calls = []

    batch = verify_submission_addresses(
        [first, second],
        lambda postcode: calls.append(postcode) or _Response(200, _one_candidate()),
    )

    assert calls == ["SW1A 1AA"]
    assert batch.unique_postcode_lookups == 1
    assert batch.cache_hits == 1

    time = [0.0]
    delays = []
    limiter = RateLimitedPostcodeLookup(
        lambda _: _Response(200, {}),
        now=lambda: time[0],
        sleeper=lambda delay: (delays.append(delay), time.__setitem__(0, time[0] + delay)),
    )
    limiter.get("SW1A 1AA")
    limiter.get("SW1A 2AA")
    assert delays == [1.25]


def test_verification_handles_invalid_postcodes_and_unavailable_service_without_changes():
    invalid = _submission(Address(index=1, line_1="Court", postcode="NOT A POSTCODE"))
    unavailable = CourtSubmission(
        source=SourceMetadata(source_row_number=3),
        court_slug="other-court",
        addresses=[Address(index=1, line_1="Court", postcode="SW1A 1AA")],
    )
    calls = []

    def lookup(postcode):
        calls.append(postcode)
        raise RuntimeError("offline")

    batch = verify_submission_addresses([invalid, unavailable], lookup)

    assert batch.verifications[0].status == "invalid_postcode"
    assert batch.action_reason_for(invalid, 1) is not None
    assert batch.verifications[1].status == "unavailable"
    assert batch.action_reason_for(unavailable, 1) is None
    assert batch.as_dict()["action_blocking_count"] == 1
    assert batch.summary_metrics()["address_verification_action_blocking_count"] == 1
    assert batch.summary_metrics()["address_verification_action_blocking_submission_count"] == 1
    assert calls == ["SW1A 1AA"]


def test_scottish_postcode_reaches_the_os_lookup():
    submission = _submission(
        Address(
            index=1,
            line_1="Court House",
            town_or_city="Aberdeen",
            postcode="AB10 1SH",
        )
    )
    calls = []

    batch = verify_submission_addresses(
        [submission], lambda postcode: calls.append(postcode) or _Response(200, {})
    )

    assert calls == ["AB10 1SH"]
    assert batch.verifications[0].status == "no_os_result"


@pytest.mark.parametrize("line", ["PO Box 12", "P.O. Box 12", "P O Box 12", "P.O Box 12"])
def test_po_box_variants_follow_the_ordinary_os_lookup(line):
    submission = _submission(
        Address(index=1, address_type="Visit", line_1=line, postcode="SW1A 1AA")
    )
    calls = []

    batch = verify_submission_addresses(
        [submission], lambda postcode: calls.append(postcode) or _Response(200, {})
    )

    verification = batch.verifications[0]
    assert verification.status == "no_os_result"
    assert verification.original_address["line_1"] == line
    assert verification.candidates == []
    assert calls == ["SW1A 1AA"]


def _submission(address: Address) -> CourtSubmission:
    return CourtSubmission(
        source=SourceMetadata(source_row_number=2),
        court_slug="example-court",
        addresses=[address],
        raw={"AA": "Court House, 1 Main Street"},
        cleaned={"before": "address-verification"},
    )


def _one_candidate():
    return {"results": [_dpa("uprn-1", organisation_name="Court House", building_number="1", thoroughfare_name="Main Street")]}


def _dpa(uprn, organisation_name=None, building_number=None, thoroughfare_name=None):
    return {
        "DPA": {
            "UPRN": uprn,
            "ADDRESS": "Court House, 1 Main Street, London, SW1A 1AA",
            "ORGANISATION_NAME": organisation_name,
            "BUILDING_NUMBER": building_number,
            "THOROUGHFARE_NAME": thoroughfare_name,
            "POST_TOWN": "London",
            "POSTCODE": "SW1A 1AA",
        }
    }
