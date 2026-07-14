import json

from fact_form_importer.execution.approvals import LlmApprovalStore
from fact_form_importer.llm.prompts import SYSTEM_PROMPT
from fact_form_importer.llm.review import (
    address_review_id,
    build_llm_actions_review,
    field_review_id,
    load_or_derive_llm_actions_review,
)
from fact_form_importer.models.court_submission import Address, CourtSubmission
from fact_form_importer.models.source import SourceMetadata
from fact_form_importer.validators.os_addresses import (
    AddressVerification,
    AddressVerificationBatch,
    OsAddressCandidate,
)


def test_prompt_treats_line_one_as_weaker_without_allowing_conflicting_matches():
    assert "line_1 as a weaker matching signal" in SYSTEM_PROMPT
    assert "non-line_1 details conflict" in SYSTEM_PROMPT
    assert "candidate remains plausible" in SYSTEM_PROMPT


def test_review_report_records_field_and_barnet_style_address_evidence(tmp_path):
    submission = CourtSubmission(
        source=SourceMetadata(source_row_number=4),
        court_slug="barnet-civil-and-family-courts-centre",
        status="processed",
        raw={"AA": "St Mary's Court", "AB": "Regents Park Road"},
        facilities={"accessible_toilet_description": "Accessible on floor two."},
        addresses=[
            Address(
                index=1,
                address_type="Visit and send documents to",
                line_1="St Mary's Court",
                line_2="Regents Park Road",
                town_or_city="London",
                postcode="N3 1BQ",
            )
        ],
    )
    candidate = OsAddressCandidate(
        uprn="200222235",
        address="BARNET COUNTY COURT, ST. MARYS COURT, REGENTS PARK ROAD, LONDON, N3 1BQ",
        organisation_name="BARNET COUNTY COURT",
        building_number=None,
        building_name="ST. MARYS COURT",
        thoroughfare_name="REGENTS PARK ROAD",
        post_town="LONDON",
        postcode="N3 1BQ",
    )
    verification = AddressVerification(
        source_row_number=4,
        court_slug=submission.court_slug,
        address_index=1,
        postcode="N3 1BQ",
        status="review_required",
        message="No unique deterministic match",
        original_address=submission.addresses[0].model_dump(mode="json"),
        candidates=[candidate],
        llm_suggestion={
            "uprn": "200222235",
            "confidence": "high",
            "needs_human_review": False,
            "reason": "The remaining address fields consistently identify this candidate.",
        },
    )
    field_result = {
        "source_row_number": 4,
        "court_slug": submission.court_slug,
        "field": "facilities.accessible_toilet_description",
        "llm_input": {"raw_value": "accessible floor 2", "cleaned_value": "accessible floor 2"},
        "model_result": {
            "value": "Accessible on floor two.",
            "confidence": "medium",
            "needs_human_review": False,
            "reason": "Made suitable for public display.",
        },
        "outcome": "accepted",
    }
    manifest = {
        "records": [
            {
                "source_row_numbers": [4],
                "actions": [
                    {
                        "action_id": "barnet-1",
                        "source_fields": ["facilities.accessible_toilet_description"],
                    },
                    {
                        "action_id": "barnet-2",
                        "source_fields": ["addresses[1]"],
                    },
                ],
            }
        ]
    }

    report = build_llm_actions_review(
        [submission],
        [field_result],
        AddressVerificationBatch(enabled=True, verifications=[verification]),
        manifest,
        mapping_path=tmp_path / "missing-mapping.json",
    )

    field_item = next(item for item in report["items"] if item["kind"] == "field")
    address_item = next(item for item in report["items"] if item["kind"] == "address")
    assert field_item["review_id"] == field_review_id(4, "facilities.accessible_toilet_description")
    assert field_item["dependent_action_ids"] == ["barnet-1"]
    assert address_item["review_id"] == address_review_id(4, 1)
    assert address_item["llm_input"]["submitted_address"]["line_1"] == "St Mary's Court"
    assert address_item["llm_input"]["candidates"][0]["uprn"] == "200222235"
    assert address_item["selected_candidate"]["organisation_name"] == "BARNET COUNTY COURT"
    assert address_item["proposed_address"]["line_1"] == "BARNET COUNTY COURT"
    assert address_item["proposed_address"]["line_2"] == "ST. MARYS COURT REGENTS PARK ROAD"
    assert address_item["api_body_patch"]["townCity"] == "LONDON"
    assert address_item["dependent_action_ids"] == ["barnet-2"]
    assert report["actionable_item_count"] == 2


def test_approval_store_is_idempotent_and_legacy_report_is_derived(tmp_path):
    store = LlmApprovalStore(tmp_path)
    first = store.approve("run-1", "review-1")
    second = store.approve("run-1", "review-1")

    assert first.approvals["review-1"].approved_at == second.approvals["review-1"].approved_at
    assert (
        json.loads(store.path_for("run-1").read_text())["approvals"]["review-1"]["review_id"]
        == "review-1"
    )

    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "submissions_cleaned.json").write_text(
        json.dumps(
            [
                {
                    "source": {"source_row_number": 2},
                    "court_slug": "example-court",
                    "raw": {"A": "source"},
                    "issues": [
                        {
                            "field": "facilities.accessible_toilet_description",
                            "code": "LLM_FIELD_NORMALISED",
                            "raw_value": "near reception",
                            "cleaned_value": "Near reception.",
                        }
                    ],
                }
            ]
        )
    )
    (archive / "api_readiness_report.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "source_row_numbers": [2],
                        "actions": [
                            {
                                "action_id": "example-court-1",
                                "source_fields": ["facilities.accessible_toilet_description"],
                            }
                        ],
                    }
                ]
            }
        )
    )
    (archive / "address_verification_report.json").write_text("{}")

    report = load_or_derive_llm_actions_review(archive)

    assert report["derived_from_legacy_archive"] is True
    assert report["items"][0]["actionable"] is True
    assert report["items"][0]["model_result"]["reason"] == "Unavailable for this legacy run"
