"""Immutable LLM review evidence and stable approval dependencies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from fact_form_importer.ingest.column_mapping import load_column_mapping
from fact_form_importer.models.court_submission import Address, CourtSubmission
from fact_form_importer.validators.os_addresses import (
    AddressVerification,
    AddressVerificationBatch,
    OsAddressCandidate,
)

LLM_ACTIONS_REVIEW_VERSION = "1.0"
LLM_ACTIONS_REVIEW_NAME = "llm_actions_review.json"
LLM_FIELD_NORMALISED = "LLM_FIELD_NORMALISED"


def field_review_id(source_row_number: int, field: str) -> str:
    return _stable_id("field", source_row_number, field)


def address_review_id(source_row_number: int, address_index: int) -> str:
    return _stable_id("address", source_row_number, str(address_index))


def _stable_id(kind: str, source_row_number: int, value: str) -> str:
    digest = hashlib.sha256(f"{kind}|{source_row_number}|{value}".encode("utf-8")).hexdigest()[:16]
    return f"llm-{kind}-{source_row_number}-{digest}"


def usable_address_review(verification: AddressVerification) -> dict[str, Any] | None:
    """Return the safe OS mapping selected by the LLM, if one exists."""

    suggestion = verification.llm_suggestion or {}
    uprn = suggestion.get("uprn")
    if not isinstance(uprn, str) or not uprn:
        return None
    selected = next(
        (candidate for candidate in verification.candidates if candidate.uprn == uprn),
        None,
    )
    if selected is None:
        return None
    try:
        source = Address.model_validate(verification.original_address)
    except (TypeError, ValueError):
        return None
    patch = selected.proposed_address(source)
    if patch is None:
        return None
    proposed = {**verification.original_address, **patch}
    return {
        "review_id": address_review_id(verification.source_row_number, verification.address_index),
        "selected_candidate": selected.as_dict(),
        "proposed_address": proposed,
        "api_body_patch": _address_api_body_patch(patch),
    }


def build_llm_actions_review(
    submissions: list[CourtSubmission],
    field_results: list[dict[str, Any]],
    address_verifications: AddressVerificationBatch,
    manifest: dict[str, Any],
    *,
    mapping_path: Path | None = None,
) -> dict[str, Any]:
    """Build the versioned review artifact after API actions are known."""

    submissions_by_row = {
        submission.source.source_row_number: submission for submission in submissions
    }
    actions_by_row = _actions_by_row(manifest)
    items: list[dict[str, Any]] = []

    for result in field_results:
        row = int(result["source_row_number"])
        field = str(result.get("field") or "")
        dependencies = _dependent_actions(field, actions_by_row.get(row, []))
        submission = submissions_by_row.get(row)
        item = {
            **result,
            "review_id": field_review_id(row, field),
            "kind": "field",
            "source_raw_values": _raw_values_for_fields(submission, [field], mapping_path),
            "dependent_action_ids": dependencies,
        }
        item["actionable"] = bool(item.get("outcome") == "accepted" and dependencies)
        items.append(item)

    for verification in address_verifications.verifications:
        if verification.status != "review_required" or not verification.candidates:
            continue
        actions = actions_by_row.get(verification.source_row_number, [])
        dependencies = _dependent_actions(f"addresses[{verification.address_index}]", actions)
        submission = submissions_by_row.get(verification.source_row_number)
        items.append(
            _address_item(
                verification,
                dependencies,
                _raw_values_for_fields(
                    submission,
                    [f"addresses[{verification.address_index}]"],
                    mapping_path,
                ),
            )
        )

    return _report(items)


def load_or_derive_llm_actions_review(archive_path: Path) -> dict[str, Any]:
    """Load a new review artifact or derive the best safe legacy equivalent."""

    report_path = archive_path / LLM_ACTIONS_REVIEW_NAME
    if report_path.exists():
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload

    submissions = _read_json(archive_path / "submissions_cleaned.json", [])
    manifest = _read_json(archive_path / "api_readiness_report.json", {})
    address_report = _read_json(archive_path / "address_verification_report.json", {})
    actions_by_row = _actions_by_row(manifest)
    items: list[dict[str, Any]] = []

    for submission in submissions if isinstance(submissions, list) else []:
        row = submission.get("source", {}).get("source_row_number")
        if not isinstance(row, int):
            continue
        issues = submission.get("issues", [])
        for issue in issues:
            if issue.get("code") != LLM_FIELD_NORMALISED:
                continue
            field = str(issue.get("field") or "")
            confidence_issue = next(
                (
                    candidate
                    for candidate in issues
                    if candidate.get("field") == field
                    and candidate.get("code") == "LLM_LOW_CONFIDENCE"
                ),
                None,
            )
            review_issue = next(
                (
                    candidate
                    for candidate in issues
                    if candidate.get("field") == field
                    and candidate.get("code") == "LLM_REVIEW_REQUIRED"
                ),
                None,
            )
            dependencies = _dependent_actions(field, actions_by_row.get(row, []))
            confidence = (
                confidence_issue.get("cleaned_value")
                if isinstance(confidence_issue, dict)
                and confidence_issue.get("cleaned_value") in {"high", "medium", "low"}
                else "unavailable"
            )
            items.append(
                {
                    "review_id": field_review_id(row, field),
                    "kind": "field",
                    "source_row_number": row,
                    "court_slug": submission.get("court_slug"),
                    "field": field,
                    "llm_input": {
                        "raw_value": issue.get("raw_value"),
                        "cleaned_value": issue.get("raw_value"),
                    },
                    "model_result": {
                        "value": issue.get("cleaned_value"),
                        "confidence": confidence,
                        "needs_human_review": bool(review_issue),
                        "reason": "Unavailable for this legacy run",
                    },
                    "outcome": "accepted",
                    "source_raw_values": _legacy_raw_values(submission, field),
                    "dependent_action_ids": dependencies,
                    "actionable": bool(dependencies),
                    "legacy": True,
                }
            )

    verifications = (
        address_report.get("verifications", []) if isinstance(address_report, dict) else []
    )
    for value in verifications:
        if not isinstance(value, dict) or not value.get("candidates"):
            continue
        verification = _verification_from_dict(value)
        if verification is None:
            continue
        row = verification.source_row_number
        field = f"addresses[{verification.address_index}]"
        dependencies = _dependent_actions(field, actions_by_row.get(row, []))
        submission = next(
            (
                candidate
                for candidate in submissions
                if candidate.get("source", {}).get("source_row_number") == row
            ),
            {},
        )
        item = _address_item(
            verification,
            dependencies,
            _legacy_raw_values(submission, field),
        )
        item["legacy"] = True
        items.append(item)

    payload = _report(items)
    payload["derived_from_legacy_archive"] = True
    return payload


def accepted_review_ids_for_fields(
    field_results: Iterable[dict[str, Any]], source_fields: list[str]
) -> list[str]:
    ids = []
    for result in field_results:
        if result.get("outcome") != "accepted":
            continue
        field = str(result.get("field") or "")
        if any(_paths_overlap(field, source_field) for source_field in source_fields):
            ids.append(field_review_id(int(result["source_row_number"]), field))
    return sorted(set(ids))


def _address_item(
    verification: AddressVerification,
    dependencies: list[str],
    source_raw_values: dict[str, Any],
) -> dict[str, Any]:
    usable = usable_address_review(verification)
    suggestion = verification.llm_suggestion
    exact_request = {
        "address_index": verification.address_index,
        "submitted_address": {
            key: verification.original_address.get(key)
            for key in ("line_1", "line_2", "town_or_city", "county")
        },
        "candidates": [
            {
                key: candidate.as_dict().get(key)
                for key in (
                    "uprn",
                    "organisation_name",
                    "building_number",
                    "building_name",
                    "thoroughfare_name",
                    "post_town",
                )
            }
            for candidate in verification.candidates
            if candidate.uprn
        ],
    }
    outcome = "accepted" if usable else "no_selection"
    if suggestion is None:
        outcome = "no_result"
    item = {
        "review_id": address_review_id(verification.source_row_number, verification.address_index),
        "kind": "address",
        "source_row_number": verification.source_row_number,
        "court_slug": verification.court_slug,
        "field": f"addresses[{verification.address_index}]",
        "address_index": verification.address_index,
        "source_raw_values": source_raw_values,
        "submitted_address": verification.original_address,
        "llm_input": exact_request,
        "os_candidates": [candidate.as_dict() for candidate in verification.candidates],
        "model_result": suggestion
        or {
            "uprn": None,
            "confidence": "unavailable",
            "needs_human_review": True,
            "reason": "The LLM did not return a recorded address result",
        },
        "selected_candidate": usable.get("selected_candidate") if usable else None,
        "proposed_address": usable.get("proposed_address") if usable else None,
        "api_body_patch": usable.get("api_body_patch") if usable else None,
        "outcome": outcome,
        "dependent_action_ids": dependencies,
        "actionable": bool(usable and dependencies),
    }
    return item


def _report(items: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        items,
        key=lambda item: (
            int(item.get("source_row_number") or 0),
            0 if item.get("kind") == "field" else 1,
            str(item.get("field") or ""),
        ),
    )
    return {
        "review_version": LLM_ACTIONS_REVIEW_VERSION,
        "item_count": len(ordered),
        "field_item_count": sum(item.get("kind") == "field" for item in ordered),
        "address_item_count": sum(item.get("kind") == "address" for item in ordered),
        "actionable_item_count": sum(bool(item.get("actionable")) for item in ordered),
        "items": ordered,
    }


def _actions_by_row(manifest: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    for record in manifest.get("records", []) if isinstance(manifest, dict) else []:
        for row in record.get("source_row_numbers", []):
            if isinstance(row, int):
                result.setdefault(row, []).extend(record.get("actions", []))
    return result


def _dependent_actions(field: str, actions: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(action["action_id"])
            for action in actions
            if action.get("action_id")
            and any(
                _paths_overlap(field, str(source_field))
                for source_field in action.get("source_fields", [])
            )
        }
    )


def _paths_overlap(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(right + ".")
        or left.startswith(right + "[")
        or right.startswith(left + ".")
        or right.startswith(left + "[")
    )


def _address_api_body_patch(patch: dict[str, Any]) -> dict[str, Any]:
    return {
        "addressLine1": patch.get("line_1"),
        "addressLine2": patch.get("line_2"),
        "townCity": patch.get("town_or_city"),
        "county": patch.get("county"),
        "postcode": patch.get("postcode"),
    }


def _raw_values_for_fields(
    submission: CourtSubmission | None,
    source_fields: list[str],
    mapping_path: Path | None,
) -> dict[str, Any]:
    if submission is None:
        return {}
    raw = submission.raw if isinstance(submission.raw, dict) else {}
    path = mapping_path or Path(__file__).resolve().parents[2] / "config" / "column_mapping.json"
    if not path.exists():
        return raw
    mapping = load_column_mapping(path)
    columns: set[str] = set()
    for field in source_fields:
        root = field.split("[", 1)[0].split(".", 1)[0]
        child = field.split(".", 1)[1] if "." in field else ""
        if root == "facilities" and child:
            columns.update(ref.column for ref in mapping.scalars if ref.field == child)
        elif root == "counter_service":
            columns.update(ref.column for ref in mapping.counter_service)
        elif root in {"addresses", "contacts", "opening_hours"}:
            index_text = field.split("[", 1)[1].split("]", 1)[0] if "[" in field else ""
            if not index_text.isdigit():
                continue
            groups = {
                "addresses": mapping.address_groups,
                "contacts": mapping.contact_detail_groups,
                "opening_hours": mapping.opening_hours_groups,
            }[root]
            group = next((item for item in groups if item.index == int(index_text)), None)
            if group:
                columns.update(ref.column for ref in group.columns)
    return {column: raw.get(column) for column in sorted(columns) if raw.get(column) is not None}


def _legacy_raw_values(submission: dict[str, Any], field: str) -> dict[str, Any]:
    raw = submission.get("raw", {}) if isinstance(submission, dict) else {}
    if not isinstance(raw, dict):
        return {}
    # Legacy archives do not contain an exact request artifact. Keep the value
    # useful without copying hundreds of unrelated spreadsheet columns.
    return {"record_raw_data_available": True, "field": field} if raw else {}


def _verification_from_dict(value: dict[str, Any]) -> AddressVerification | None:
    try:
        candidates = [
            OsAddressCandidate(**candidate)
            for candidate in value.get("candidates", [])
            if isinstance(candidate, dict)
        ]
        selected_value = value.get("selected_candidate")
        selected = (
            OsAddressCandidate(**selected_value) if isinstance(selected_value, dict) else None
        )
        return AddressVerification(
            source_row_number=int(value["source_row_number"]),
            court_slug=value.get("court_slug"),
            address_index=int(value["address_index"]),
            postcode=value.get("postcode"),
            status=value.get("status", "review_required"),
            message=str(value.get("message") or ""),
            original_address=value.get("original_address") or {},
            proposed_address=value.get("proposed_address"),
            selected_candidate=selected,
            candidates=candidates,
            match_score=value.get("match_score"),
            score_margin=value.get("score_margin"),
            match_type=value.get("match_type"),
            llm_suggestion=value.get("llm_suggestion"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
