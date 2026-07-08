"""Read Microsoft Forms exports into CourtSubmission models."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fact_form_importer.cleaners.booleans import normalise_yes_no
from fact_form_importer.cleaners.emails import normalise_email
from fact_form_importer.cleaners.multiselect import split_multiselect
from fact_form_importer.cleaners.phones import normalise_uk_phone
from fact_form_importer.cleaners.postcodes import normalise_uk_postcode
from fact_form_importer.cleaners.slug import normalise_court_slug
from fact_form_importer.cleaners.strings import null_if_empty_like
from fact_form_importer.cleaners.times import parse_time_parts
from fact_form_importer.ingest.column_mapping import (
    ColumnMapping,
    ColumnRef,
    RepeatedGroup,
    build_raw_row,
    get_cell,
    load_column_mapping,
)
from fact_form_importer.ingest.workbook_profiler import excel_column_letter, read_workbook_rows
from fact_form_importer.models.court_submission import (
    Address,
    ContactDetail,
    CourtSubmission,
    OpeningHoursSet,
    OpeningTime,
)
from fact_form_importer.models.issues import Issue
from fact_form_importer.models.source import SourceMetadata

DEFAULT_COLUMN_MAPPING_PATH = Path("config/column_mapping.json")

YES_NO_SCALAR_FIELDS = {
    "accessible_parking",
    "accessible_entrance",
    "lift_available",
    "quiet_room_available",
    "parking_available",
    "separate_waiting_areas",
    "child_waiting_area",
    "quiet_room_available_2",
    "baby_changing",
    "wifi_available",
}
PHONE_SCALAR_FIELDS = {
    "accessible_parking_phone",
    "accessible_entrance_support_phone",
}
MULTISELECT_SCALAR_FIELDS = {"food_and_drink"}
BUSINESS_GROUP_IGNORED_FIELDS = {"add_another", "add_another_after_days"}


@dataclass
class IngestResult:
    submissions: list[CourtSubmission] = field(default_factory=list)
    skipped_empty_rows: int = 0
    mapping_warnings: list[dict[str, Any]] = field(default_factory=list)


def ingest_workbook(
    input_path: Path,
    output_path: Optional[Path] = None,
    column_mapping_path: Path = DEFAULT_COLUMN_MAPPING_PATH,
) -> IngestResult:
    mapping = load_column_mapping(column_mapping_path)
    _, rows = read_workbook_rows(input_path)
    if not rows:
        result = IngestResult()
        if output_path is not None:
            write_ingest_outputs(result, output_path)
        return result

    header_row = rows[0]
    headers_by_column = {
        excel_column_letter(index): value for index, value in enumerate(header_row)
    }
    mapping_warnings = [
        warning.model_dump(mode="json") for warning in mapping.validate_headers(headers_by_column)
    ]

    result = IngestResult(mapping_warnings=mapping_warnings)
    for row_offset, row in enumerate(rows[1:], start=2):
        raw_row = build_raw_row(row)
        if _is_empty_business_row(raw_row, mapping):
            result.skipped_empty_rows += 1
            continue

        result.submissions.append(_build_submission(raw_row, row_offset, mapping))

    if output_path is not None:
        write_ingest_outputs(result, output_path)

    return result


def write_ingest_outputs(result: IngestResult, output_path: Path) -> None:
    output_path.mkdir(parents=True, exist_ok=True)

    raw_payload = [
        {
            "source": submission.source.model_dump(mode="json"),
            "raw": submission.raw,
            "issues": [issue.model_dump(mode="json") for issue in submission.issues],
            "status": submission.status,
        }
        for submission in result.submissions
    ]
    cleaned_payload = [submission.model_dump(mode="json") for submission in result.submissions]
    summary_payload = {
        "submissions_total": len(result.submissions),
        "skipped_empty_rows": result.skipped_empty_rows,
        "failed": sum(1 for submission in result.submissions if submission.status == "failed"),
        "needs_human_review": sum(
            1 for submission in result.submissions if submission.status == "needs_human_review"
        ),
        "processed": sum(1 for submission in result.submissions if submission.status == "processed"),
        "processed_with_warnings": sum(
            1 for submission in result.submissions if submission.status == "processed_with_warnings"
        ),
        "mapping_warnings": result.mapping_warnings,
    }

    _write_json(output_path / "submissions_raw.json", raw_payload)
    _write_json(output_path / "submissions_cleaned.json", cleaned_payload)
    _write_json(output_path / "ingest_summary.json", summary_payload)


def _build_submission(
    raw_row: dict[str, Any],
    source_row_number: int,
    mapping: ColumnMapping,
) -> CourtSubmission:
    issues: list[Issue] = []
    source = _build_source_metadata(raw_row, source_row_number, mapping)
    scalar_refs = {column_ref.field: column_ref for column_ref in mapping.scalars}

    court_slug_raw = null_if_empty_like(get_cell(raw_row, scalar_refs["court_slug_raw"].column))
    court_slug = normalise_court_slug(court_slug_raw)

    if court_slug_raw is not None and court_slug is None:
        issues.append(
            Issue(
                field="court_slug",
                code="INVALID_COURT_IDENTIFIER",
                severity="error",
                message="Court identifier could not be normalised to a slug",
                raw_value=court_slug_raw,
                cleaned_value=None,
            )
        )

    facilities = _build_facilities(raw_row, mapping, issues)
    translation_phone = _clean_phone_ref(raw_row, scalar_refs["translation_phone"], issues)
    translation_email = _clean_email_ref(raw_row, scalar_refs["translation_email"], issues)
    addresses = _build_addresses(raw_row, mapping, issues)
    counter_service = _build_counter_service(raw_row, mapping, issues)
    interview_rooms = _build_interview_rooms(raw_row, mapping, issues)
    contacts = _build_contacts(raw_row, mapping, issues)
    opening_hours = _build_opening_hours(raw_row, mapping, issues)

    if court_slug_raw is None:
        issues.append(
            Issue(
                field="court_slug_raw",
                code="MISSING_COURT_IDENTIFIER",
                severity="error",
                message="Court identifier is missing but business data is present",
                raw_value=None,
                cleaned_value=None,
            )
        )

    status = _submission_status(issues)
    cleaned = {
        "court_slug": court_slug,
        "facilities": facilities,
        "translation_phone": translation_phone,
        "translation_email": translation_email,
        "addresses": [address.model_dump(mode="json") for address in addresses],
        "counter_service": counter_service,
        "interview_rooms": interview_rooms,
        "contacts": [contact.model_dump(mode="json") for contact in contacts],
        "opening_hours": [hours.model_dump(mode="json") for hours in opening_hours],
    }

    return CourtSubmission(
        source=source,
        court_slug_raw=court_slug_raw,
        court_slug=court_slug,
        facilities=facilities,
        translation_phone=translation_phone,
        translation_email=translation_email,
        addresses=addresses,
        counter_service=counter_service,
        interview_rooms=interview_rooms,
        contacts=contacts,
        opening_hours=opening_hours,
        raw=raw_row,
        cleaned=cleaned,
        issues=issues,
        status=status,
    )


def _build_source_metadata(
    raw_row: dict[str, Any],
    source_row_number: int,
    mapping: ColumnMapping,
) -> SourceMetadata:
    refs = {column_ref.field: column_ref for column_ref in mapping.metadata}
    return SourceMetadata(
        source_row_number=source_row_number,
        forms_id=_clean_string_ref(raw_row, refs["forms_id"]),
        start_time=_clean_string_ref(raw_row, refs["start_time"]),
        completion_time=_clean_string_ref(raw_row, refs["completion_time"]),
        submitter_email=_clean_string_ref(raw_row, refs["submitter_email"]),
        submitter_name=_clean_string_ref(raw_row, refs["submitter_name"]),
        last_modified_time=_clean_string_ref(raw_row, refs["last_modified_time"]),
    )


def _build_facilities(
    raw_row: dict[str, Any],
    mapping: ColumnMapping,
    issues: list[Issue],
) -> dict[str, Any]:
    facilities: dict[str, Any] = {}

    for column_ref in mapping.scalars:
        if column_ref.field in {"court_slug_raw", "translation_phone", "translation_email"}:
            continue

        if column_ref.field in YES_NO_SCALAR_FIELDS:
            result = normalise_yes_no(get_cell(raw_row, column_ref.column), column_ref.field)
            facilities[column_ref.field] = result.value
            issues.extend(result.issues)
        elif column_ref.field in PHONE_SCALAR_FIELDS:
            facilities[column_ref.field] = _clean_phone_ref(raw_row, column_ref, issues)
        elif column_ref.field in MULTISELECT_SCALAR_FIELDS:
            facilities[column_ref.field] = split_multiselect(get_cell(raw_row, column_ref.column))
        else:
            facilities[column_ref.field] = _clean_string_ref(raw_row, column_ref)

    return facilities


def _build_addresses(
    raw_row: dict[str, Any],
    mapping: ColumnMapping,
    issues: list[Issue],
) -> list[Address]:
    addresses: list[Address] = []
    for group in mapping.address_groups:
        if not _group_has_meaningful_data(raw_row, group):
            continue

        refs = {column_ref.field: column_ref for column_ref in group.columns}
        postcode = None
        if "postcode" in refs:
            result = normalise_uk_postcode(
                get_cell(raw_row, refs["postcode"].column),
                f"addresses[{group.index}].postcode",
            )
            postcode = result.value
            issues.extend(result.issues)

        addresses.append(
            Address(
                index=group.index,
                address_type=_clean_string_ref(raw_row, refs["address_type"]),
                line_1=_clean_string_ref(raw_row, refs["line_1"]),
                line_2=_clean_string_ref(raw_row, refs["line_2"]),
                town_or_city=_clean_string_ref(raw_row, refs["town_or_city"]),
                county=_clean_string_ref(raw_row, refs["county"]),
                postcode=postcode,
                areas_of_law=split_multiselect(get_cell(raw_row, refs["areas_of_law"].column)),
                court_types=split_multiselect(get_cell(raw_row, refs["court_types"].column)),
            )
        )

    return addresses


def _build_counter_service(
    raw_row: dict[str, Any],
    mapping: ColumnMapping,
    issues: list[Issue],
) -> dict[str, Any]:
    refs = {column_ref.field: column_ref for column_ref in mapping.counter_service}
    same_result = normalise_yes_no(
        get_cell(raw_row, refs["same_monday_to_friday"].column),
        "counter_service.same_monday_to_friday",
    )
    issues.extend(same_result.issues)

    return {
        "specific_courts": split_multiselect(get_cell(raw_row, refs["specific_courts"].column)),
        "assists_with": split_multiselect(get_cell(raw_row, refs["assists_with"].column)),
        "appointment_contact": _clean_string_ref(raw_row, refs["appointment_contact"]),
        "same_monday_to_friday": same_result.value,
        "monday_to_friday": _build_time_from_refs(
            raw_row, refs, "", "counter_service", issues, strip_midnight_placeholder=True
        ),
        "monday": _build_time_from_refs(
            raw_row,
            refs,
            "monday_",
            "counter_service.monday",
            issues,
            strip_midnight_placeholder=True,
        ),
        "tuesday": _build_time_from_refs(
            raw_row,
            refs,
            "tuesday_",
            "counter_service.tuesday",
            issues,
            strip_midnight_placeholder=True,
        ),
        "wednesday": _build_time_from_refs(
            raw_row,
            refs,
            "wednesday_",
            "counter_service.wednesday",
            issues,
            strip_midnight_placeholder=True,
        ),
        "thursday": _build_time_from_refs(
            raw_row,
            refs,
            "thursday_",
            "counter_service.thursday",
            issues,
            strip_midnight_placeholder=True,
        ),
        "friday": _build_time_from_refs(
            raw_row,
            refs,
            "friday_",
            "counter_service.friday",
            issues,
            strip_midnight_placeholder=True,
        ),
    }


def _build_interview_rooms(
    raw_row: dict[str, Any],
    mapping: ColumnMapping,
    issues: list[Issue],
) -> dict[str, Any]:
    refs = {column_ref.field: column_ref for column_ref in mapping.interview_rooms}
    result = normalise_yes_no(
        get_cell(raw_row, refs["has_interview_rooms"].column),
        "interview_rooms.has_interview_rooms",
    )
    issues.extend(result.issues)
    return {
        "has_interview_rooms": result.value,
        "room_count": _clean_string_ref(raw_row, refs["room_count"]),
        "booking_phone": _clean_phone_ref(raw_row, refs["booking_phone"], issues),
    }


def _build_contacts(
    raw_row: dict[str, Any],
    mapping: ColumnMapping,
    issues: list[Issue],
) -> list[ContactDetail]:
    contacts: list[ContactDetail] = []
    for group in mapping.contact_detail_groups:
        if not _group_has_meaningful_data(raw_row, group):
            continue

        refs = {column_ref.field: column_ref for column_ref in group.columns}
        contacts.append(
            ContactDetail(
                index=group.index,
                description=_clean_string_ref(raw_row, refs["description"]),
                explanation=_clean_string_ref(raw_row, refs["explanation"]),
                phone=_clean_phone_ref(raw_row, refs["phone"], issues),
                email=_clean_email_ref(raw_row, refs["email"], issues),
            )
        )

    return contacts


def _build_opening_hours(
    raw_row: dict[str, Any],
    mapping: ColumnMapping,
    issues: list[Issue],
) -> list[OpeningHoursSet]:
    opening_hours: list[OpeningHoursSet] = []
    for group in mapping.opening_hours_groups:
        if not _group_has_meaningful_data(raw_row, group):
            continue

        refs = {column_ref.field: column_ref for column_ref in group.columns}
        same_result = normalise_yes_no(
            get_cell(raw_row, refs["same_monday_to_friday"].column),
            f"opening_hours[{group.index}].same_monday_to_friday",
        )
        issues.extend(same_result.issues)

        opening_hours.append(
            OpeningHoursSet(
                index=group.index,
                type=_clean_string_ref(raw_row, refs["type"]),
                same_monday_to_friday=same_result.value,
                monday_to_friday=_build_time_from_refs(
                    raw_row, refs, "", f"opening_hours[{group.index}]", issues
                ),
                monday=_build_time_from_refs(
                    raw_row, refs, "monday_", f"opening_hours[{group.index}].monday", issues
                ),
                tuesday=_build_time_from_refs(
                    raw_row, refs, "tuesday_", f"opening_hours[{group.index}].tuesday", issues
                ),
                wednesday=_build_time_from_refs(
                    raw_row,
                    refs,
                    "wednesday_",
                    f"opening_hours[{group.index}].wednesday",
                    issues,
                ),
                thursday=_build_time_from_refs(
                    raw_row, refs, "thursday_", f"opening_hours[{group.index}].thursday", issues
                ),
                friday=_build_time_from_refs(
                    raw_row, refs, "friday_", f"opening_hours[{group.index}].friday", issues
                ),
            )
        )

    return opening_hours


def _build_time_from_refs(
    raw_row: dict[str, Any],
    refs: dict[str, ColumnRef],
    prefix: str,
    field_prefix: str,
    issues: list[Issue],
    strip_midnight_placeholder: bool = False,
) -> Optional[OpeningTime]:
    required_fields = [
        f"{prefix}opening_hour",
        f"{prefix}opening_minute",
        f"{prefix}closing_hour",
        f"{prefix}closing_minute",
    ]
    if not all(field in refs for field in required_fields):
        return None

    open_result = parse_time_parts(
        get_cell(raw_row, refs[f"{prefix}opening_hour"].column),
        get_cell(raw_row, refs[f"{prefix}opening_minute"].column),
        f"{field_prefix}.open",
    )
    close_result = parse_time_parts(
        get_cell(raw_row, refs[f"{prefix}closing_hour"].column),
        get_cell(raw_row, refs[f"{prefix}closing_minute"].column),
        f"{field_prefix}.close",
    )
    time_issues = open_result.issues + close_result.issues
    issues.extend(time_issues)

    if open_result.status == "empty" and close_result.status == "empty":
        return None

    if (
        strip_midnight_placeholder
        and open_result.value == "00:00"
        and close_result.value == "00:00"
        and not time_issues
    ):
        return None

    status = "valid_time"
    if open_result.status == "invalid" or close_result.status == "invalid":
        status = "invalid"

    return OpeningTime(
        open=open_result.value,
        close=close_result.value,
        status=status,
        issues=time_issues,
    )


def _clean_string_ref(raw_row: dict[str, Any], column_ref: ColumnRef) -> Optional[str]:
    return null_if_empty_like(get_cell(raw_row, column_ref.column))


def _clean_phone_ref(
    raw_row: dict[str, Any],
    column_ref: ColumnRef,
    issues: list[Issue],
) -> Optional[str]:
    result = normalise_uk_phone(get_cell(raw_row, column_ref.column), column_ref.field)
    issues.extend(result.issues)
    return result.value


def _clean_email_ref(
    raw_row: dict[str, Any],
    column_ref: ColumnRef,
    issues: list[Issue],
) -> Optional[str]:
    result = normalise_email(get_cell(raw_row, column_ref.column), column_ref.field)
    issues.extend(result.issues)
    return result.value


def _is_empty_business_row(raw_row: dict[str, Any], mapping: ColumnMapping) -> bool:
    metadata_columns = {column_ref.column for column_ref in mapping.metadata}
    business_columns = [
        column_ref
        for column_ref in mapping.expected_columns()
        if column_ref.column not in metadata_columns
    ]
    return all(
        null_if_empty_like(get_cell(raw_row, column_ref.column)) is None
        for column_ref in business_columns
    )


def _group_has_meaningful_data(raw_row: dict[str, Any], group: RepeatedGroup) -> bool:
    meaningful_columns = [
        column_ref for column_ref in group.columns if column_ref.field not in BUSINESS_GROUP_IGNORED_FIELDS
    ]
    return any(null_if_empty_like(get_cell(raw_row, column_ref.column)) is not None for column_ref in meaningful_columns)


def _submission_status(issues: list[Issue]) -> str:
    if any(issue.severity == "error" for issue in issues):
        return "failed"

    if any(issue.severity == "warning" for issue in issues):
        return "processed_with_warnings"

    return "processed"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
