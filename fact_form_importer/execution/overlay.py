"""Derive the latest run's section plan without changing its archive."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from fact_form_importer.llm.review import address_verification_batch_from_dict
from fact_form_importer.models.court_submission import CourtSubmission
from fact_form_importer.output.fact_api_manifest import build_fact_api_import_manifest
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.validators.vocabularies import Vocabularies


def derive_latest_execution_overlay(
    run_id: str,
    archive_path: Path,
    output_root: Path,
    original: dict[str, Any],
    succeeded_action_ids: set[str] | None = None,
) -> dict[str, Any]:
    directory = output_root / "execution-review-state"
    path = directory / f"{run_id}.plan.json"
    if path.exists():
        derived = json.loads(path.read_text(encoding="utf-8"))
        return _preserve_succeeded_sections(
            derived, original, succeeded_action_ids or set()
        )

    try:
        submissions = [
            CourtSubmission.model_validate(item)
            for item in _read_json(archive_path / "submissions_cleaned.json", [])
        ]
    except (TypeError, ValidationError):
        # Some early/test archives contain deliberately abbreviated evidence.
        # They remain usable through their original manifest.
        return original
    if not submissions:
        return original
    by_row = {submission.source.source_row_number: submission for submission in submissions}
    vocabularies = _derive_vocabularies(original, by_row)
    court_ids = {
        str(record.get("court_slug")): str(record.get("court_id"))
        for record in original.get("records", [])
        if record.get("court_slug") and record.get("court_id")
    }

    def lookup(slug: str) -> CourtReference:
        return CourtReference(
            court_id=court_ids.get(slug, "{court_id}"), slug=slug, name=None
        )

    address_report = _read_json(archive_path / "address_verification_report.json", {})
    llm_report = _read_json(archive_path / "llm_actions_review.json", {})
    result = build_fact_api_import_manifest(
        submissions,
        run_id,
        vocabularies,
        court_lookup=lookup,
        address_verifications=address_verification_batch_from_dict(address_report),
        llm_review_items=list(llm_report.get("items", [])),
    ).manifest.model_dump(mode="json")
    result["derived_execution_overlay"] = True
    result["source_manifest_version"] = original.get("manifest_version")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    return _preserve_succeeded_sections(result, original, succeeded_action_ids or set())


def _preserve_succeeded_sections(
    derived: dict[str, Any],
    original: dict[str, Any],
    succeeded_action_ids: set[str],
) -> dict[str, Any]:
    """Never regroup or replace a legacy section after one of its writes succeeded."""

    if not succeeded_action_ids:
        return derived
    protected: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in original.get("records", []):
        slug = str(record.get("court_slug") or "")
        actions = list(record.get("actions", []))
        protected_resources = {
            str(action.get("resource") or "")
            for action in actions
            if str(action.get("action_id") or "") in succeeded_action_ids
        }
        for resource in protected_resources:
            protected[(slug, resource)] = [
                action
                for action in actions
                if str(action.get("resource") or "") == resource
            ]
    if not protected:
        return derived
    merged = json.loads(json.dumps(derived))
    records_by_slug = {
        str(record.get("court_slug") or ""): record for record in merged.get("records", [])
    }
    original_by_slug = {
        str(record.get("court_slug") or ""): record for record in original.get("records", [])
    }
    for (slug, resource), legacy_actions in protected.items():
        record = records_by_slug.get(slug)
        if record is None:
            old_record = original_by_slug[slug]
            record = {
                **old_record,
                "actions": [],
                "derived_execution_overlay": True,
            }
            merged.setdefault("records", []).append(record)
            records_by_slug[slug] = record
        record["actions"] = [
            action
            for action in record.get("actions", [])
            if str(action.get("resource") or "") != resource
        ] + legacy_actions
    merged["preserved_succeeded_section_count"] = len(protected)
    return merged


def _derive_vocabularies(
    manifest: dict[str, Any], submissions: dict[int, CourtSubmission]
) -> Vocabularies:
    entries: dict[str, dict[str, str]] = {
        "areas_of_law": {},
        "court_types": {},
        "contact_description_types": {},
        "opening_hour_types": {},
    }
    for record in manifest.get("records", []):
        rows = record.get("source_row_numbers", [])
        submission = submissions.get(rows[0]) if len(rows) == 1 else None
        if submission is None:
            continue
        for action in record.get("actions", []):
            fields = action.get("source_fields", [])
            body = action.get("body", {})
            if action.get("resource") == "address" and fields:
                index = _field_index(fields[0])
                address = next((item for item in submission.addresses if item.index == index), None)
                if address:
                    _zip_ids(entries["areas_of_law"], address.areas_of_law, body.get("areasOfLaw"))
                    _zip_ids(entries["court_types"], address.court_types, body.get("courtTypes"))
            elif action.get("resource") == "contact_detail" and fields:
                index = _field_index(fields[0])
                contact = next((item for item in submission.contacts if item.index == index), None)
                if contact and contact.description and body.get("courtContactDescriptionId"):
                    entries["contact_description_types"][contact.description] = str(
                        body["courtContactDescriptionId"]
                    )
            elif action.get("resource") == "court_opening_hours" and fields:
                index = _field_index(fields[0])
                hours = next((item for item in submission.opening_hours if item.index == index), None)
                if hours and hours.type and body.get("openingHourTypeId"):
                    entries["opening_hour_types"][hours.type] = str(body["openingHourTypeId"])
            elif action.get("resource") == "counter_service_opening_hours":
                _zip_ids(
                    entries["court_types"],
                    submission.counter_service.get("specific_courts") or [],
                    body.get("courtTypes"),
                )
    fallback = "00000000-0000-0000-0000-000000000000"
    payload = {}
    for vocabulary, values in entries.items():
        if not values:
            values["Unavailable legacy mapping"] = fallback
        payload[vocabulary] = [
            {"code": name.casefold().replace(" ", "_"), "name": name, "api_id": api_id}
            for name, api_id in values.items()
        ]
    return Vocabularies(version="derived-overlay-1", vocabularies=payload)


def _zip_ids(target: dict[str, str], names: list[str], ids: Any) -> None:
    if not isinstance(ids, list):
        return
    for name, api_id in zip(names, ids):
        if name and api_id:
            target[str(name)] = str(api_id)


def _field_index(field: str) -> int:
    try:
        return int(field.split("[", 1)[1].split("]", 1)[0])
    except (IndexError, ValueError):
        return -1


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))
