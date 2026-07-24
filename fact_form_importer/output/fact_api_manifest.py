"""Generate an immutable, reviewable endpoint plan for guarded FaCT execution.

This module only generates actions. The separate execution service rechecks the
live court and target section before it may send any POST or PUT request.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from fact_form_importer.llm.review import (
    accepted_review_ids_for_fields,
    address_review_id,
    usable_address_review,
)
from fact_form_importer.models.court_submission import CourtSubmission, OpeningTime
from fact_form_importer.validators.base import COURT_SLUG_NOT_FOUND
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.validators.os_addresses import AddressVerificationBatch
from fact_form_importer.validators.vocabularies import Vocabularies

IMPORTABLE_STATUSES = {"processed", "processed_with_warnings"}
API_MANIFEST_VERSION = "2.7"
_MIGRATION_DEFAULT_LIFT_DOOR_WIDTH_CM = 1
_MIGRATION_DEFAULT_LIFT_DOOR_LIMIT_KG = 1
_MIGRATION_DEFAULT_INTERVIEW_ROOM_COUNT = 1
MISSING_SUPPORT_PHONE_PLACEHOLDER = "+44 0000000000"
_REVIEW_REQUIRED_MIGRATION_DEFAULT_PREFIX = "Review-required migration default:"
_UNAVAILABLE_LIFT_MEASUREMENT_MARKERS = {
    "na",
    "nk",
    "uk",
    "unknown",
    "notknown",
    "notsure",
    "dontknow",
    "donotknow",
    "notrecorded",
    "notprovided",
    "notavailable",
    "noidea",
    "noideal",
}

# These expressions deliberately mirror the public FaCT Data API request
# constraints. Keeping them here makes a rejected action reviewable before a
# mutation request is sent.
_FACT_API_ADDRESS_PATTERN = re.compile(r"^[A-Za-z0-9 ()':,.-]+$")
_FACT_API_PHONE_PATTERN = re.compile(r"^(?:\+44)?[0-9 ]{10,20}$")
_FACT_API_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$"
)
_FACT_API_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_FACT_API_CI_IOM_POSTCODE_PATTERN = re.compile(r"^(?:IM|JE|GY)", re.IGNORECASE)
_ADDRESS_CARE_OF_PATTERN = re.compile(r"\bc\s*/\s*o\b", re.IGNORECASE)
_FACT_API_OPENING_DAYS = {"EVERYDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"}
_NO_COUNTER_SERVICE_STATUSES = {
    "no counter service",
    "no counter service available",
    "counter service not available",
}

CourtLookup = Callable[[str], Optional[CourtReference]]


class FactApiAction(BaseModel):
    action_id: str
    resource: str
    method: Literal["POST", "PUT"]
    path: str
    readiness: Literal["ready", "pending"]
    body: dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = None
    preflight_required: bool = True
    source_fields: list[str] = Field(default_factory=list)
    # Keep Optional[...] rather than ``dict[...] | None``: the supported local
    # Python 3.9 environment cannot evaluate the latter inside Pydantic.
    address_verification: Optional[dict[str, Any]] = None
    request_body_normalisations: dict[str, dict[str, str]] = Field(default_factory=dict)
    migration_assumptions: list[str] = Field(default_factory=list)
    # Request-only fallbacks complete an empty FaCT section, but must not
    # overwrite a populated live value because the form did not collect them.
    fallback_fields: list[str] = Field(default_factory=list)
    llm_review_ids: list[str] = Field(default_factory=list)
    source_row_number: Optional[int] = None
    source_selection_required: bool = False
    section_id: Optional[str] = None
    proposed_items: list[dict[str, Any]] = Field(default_factory=list)
    proposed_item_ids: list[str] = Field(default_factory=list)
    proposed_item_source_fields: list[list[str]] = Field(default_factory=list)
    proposed_item_clear_fields: list[list[str]] = Field(default_factory=list)
    address_verifications: list[dict[str, Any]] = Field(default_factory=list)


class FactApiRecord(BaseModel):
    court_slug: str
    court_id: Optional[str] = None
    source_row_numbers: list[int]
    status: str
    readiness: Literal["ready", "partially_ready", "pending", "not_applicable"]
    actions: list[FactApiAction] = Field(default_factory=list)


class FactApiImportManifest(BaseModel):
    manifest_version: str = API_MANIFEST_VERSION
    execution_mode: Literal["generate_only"] = "generate_only"
    run_id: str
    records: list[FactApiRecord] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


@dataclass(frozen=True)
class FactApiManifestResult:
    manifest: FactApiImportManifest

    @property
    def metrics(self) -> dict[str, int]:
        return self.manifest.summary


def build_fact_api_import_manifest(
    submissions: list[CourtSubmission],
    run_id: str,
    vocabularies: Vocabularies | None,
    court_lookup: CourtLookup | None = None,
    address_verifications: AddressVerificationBatch | None = None,
    llm_review_items: list[dict[str, Any]] | None = None,
) -> FactApiManifestResult:
    """Build an action plan from importable submissions without executing it."""

    records = []
    for submission in submissions:
        submission_review_items = [
            item
            for item in (llm_review_items or [])
            if item.get("source_row_number") == submission.source.source_row_number
        ]
        if not _is_importable(submission, submission_review_items):
            continue
        records.append(
            _build_record(
                submission,
                vocabularies,
                court_lookup,
                address_verifications,
                submission_review_items,
            )
        )

    # Rows without a complete section remain visible in the source-review
    # report, but do not need an empty API-plan record.
    records = _merge_duplicate_records([record for record in records if record.actions])
    actions = [action for record in records for action in record.actions]
    review_required_default_actions = [
        action
        for action in actions
        if any(
            assumption.startswith(_REVIEW_REQUIRED_MIGRATION_DEFAULT_PREFIX)
            for assumption in action.migration_assumptions
        )
    ]
    summary = {
        "api_manifest_record_count": len(records),
        "api_manifest_ready_action_count": sum(action.readiness == "ready" for action in actions),
        "api_manifest_pending_action_count": sum(
            action.readiness == "pending" for action in actions
        ),
        "api_manifest_ready_record_count": sum(record.readiness == "ready" for record in records),
        "api_manifest_partially_ready_record_count": sum(
            record.readiness == "partially_ready" for record in records
        ),
        "api_manifest_pending_record_count": sum(
            record.readiness == "pending" for record in records
        ),
        "api_manifest_review_required_default_count": sum(
            sum(
                assumption.startswith(_REVIEW_REQUIRED_MIGRATION_DEFAULT_PREFIX)
                for assumption in action.migration_assumptions
            )
            for action in actions
        ),
        "api_manifest_review_required_default_action_count": len(review_required_default_actions),
        "api_manifest_awaiting_llm_approval_action_count": sum(
            bool(action.llm_review_ids) for action in actions
        ),
    }
    return FactApiManifestResult(
        manifest=FactApiImportManifest(run_id=run_id, records=records, summary=summary)
    )


def _is_importable(
    submission: CourtSubmission,
    llm_review_items: list[dict[str, Any]] | None = None,
) -> bool:
    del llm_review_items
    if not submission.court_slug:
        return False
    return not any(issue.code == COURT_SLUG_NOT_FOUND for issue in submission.issues)


def _build_record(
    submission: CourtSubmission,
    vocabularies: Vocabularies | None,
    court_lookup: CourtLookup | None,
    address_verifications: AddressVerificationBatch | None,
    llm_review_items: list[dict[str, Any]],
) -> FactApiRecord:
    court_reference = (
        court_lookup(submission.court_slug) if court_lookup and submission.court_slug else None
    )
    court_id = court_reference.court_id if court_reference else None
    actions: list[FactApiAction] = []
    action_number = 1

    def add(
        resource: str,
        method: Literal["POST", "PUT"],
        endpoint: str,
        body: dict[str, Any],
        reason: str | None = None,
        source_fields: list[str] | None = None,
        address_verification: dict[str, Any] | None = None,
        migration_assumptions: list[str] | None = None,
        fallback_fields: list[str] | None = None,
        extra_llm_review_ids: list[str] | None = None,
    ) -> None:
        nonlocal action_number
        explicit_clear_fields = _explicit_clear_fields(
            resource,
            llm_review_items,
            source_fields or [],
        )
        # FaCT validates the request object before its service layer applies the
        # court UUID from the path, so all entity request bodies must carry it.
        # Professional information uses a DTO rather than an entity and does
        # not expose a courtId field.
        if resource != "professional_information" and body:
            body = {"courtId": court_id or "{court_id}", **body}
        original_body = dict(body)
        body = normalise_fact_api_action_body(resource, body)
        if _empty_contact_item_without_clear(
            resource,
            body,
            explicit_clear_fields,
        ):
            return
        if not body:
            return
        action_reason = reason
        validation_reason = validate_fact_api_action_body(resource, body)
        action_reason = _combine_reasons(action_reason, validation_reason)
        readiness: Literal["ready", "pending"] = "pending" if action_reason else "ready"
        path = endpoint.format(court_id=court_id or "{court_id}")
        actions.append(
            FactApiAction(
                action_id=f"{submission.court_slug}-{action_number}",
                resource=resource,
                method=method,
                path=path,
                readiness=readiness,
                body=body,
                reason=action_reason,
                source_fields=source_fields or [],
                address_verification=address_verification,
                request_body_normalisations=_body_normalisations(original_body, body),
                migration_assumptions=migration_assumptions or [],
                fallback_fields=fallback_fields or [],
                llm_review_ids=sorted(
                    set(
                        accepted_review_ids_for_fields(llm_review_items, source_fields or [])
                        + (extra_llm_review_ids or [])
                    )
                ),
                source_row_number=submission.source.source_row_number,
                section_id=(
                    f"{submission.court_slug}:{resource}:{submission.source.source_row_number}"
                ),
                proposed_items=[body],
                proposed_item_ids=[
                    _proposed_item_id(
                        submission.source.source_row_number,
                        resource,
                        source_fields or [],
                    )
                ],
                proposed_item_source_fields=[source_fields or []],
                proposed_item_clear_fields=[
                    explicit_clear_fields
                ],
                address_verifications=[address_verification] if address_verification else [],
            )
        )
        action_number += 1

    add(
        "building_facilities",
        "POST",
        "/courts/{court_id}/v1/building-facilities",
        _building_facilities_body(submission.facilities),
        source_fields=[
            "facilities.parking_available",
            "facilities.quiet_room_available_2",
            "facilities.food_and_drink",
            "facilities.separate_waiting_areas",
            "facilities.child_waiting_area",
            "facilities.baby_changing",
            "facilities.wifi_available",
        ],
    )
    accessibility_body = _accessibility_options_body(submission.facilities)
    add(
        "accessibility_options",
        "POST",
        "/courts/{court_id}/v1/accessibility-options",
        accessibility_body,
        source_fields=[
            "facilities.accessible_parking",
            "facilities.accessible_parking_phone",
            "facilities.accessible_toilet_description",
            "facilities.accessible_entrance",
            "facilities.accessible_entrance_support_phone",
            "facilities.hearing_enhancement_equipment",
            "facilities.lift_available",
            "facilities.lift_door_width",
            "facilities.lift_weight_limit",
            "facilities.quiet_room_available",
        ],
        migration_assumptions=_accessibility_options_assumptions(submission.facilities),
    )
    add(
        "translation_services",
        "POST",
        "/courts/{court_id}/v1/translation-services",
        _translation_body(submission),
        source_fields=["translation_phone", "translation_email"],
    )
    add(
        "professional_information",
        "POST",
        "/courts/{court_id}/v1/professional-information",
        _professional_information_body(submission),
        source_fields=["interview_rooms"],
        migration_assumptions=_professional_information_assumptions(submission),
        fallback_fields=[
            "professionalInformation.videoHearings",
            "professionalInformation.commonPlatform",
            "professionalInformation.accessScheme",
        ],
    )

    counter_body, counter_reason = _counter_service_body(submission, vocabularies)
    add(
        "counter_service_opening_hours",
        "PUT",
        "/courts/{court_id}/v1/opening-hours/counter-service",
        counter_body,
        counter_reason,
        source_fields=["counter_service"],
        migration_assumptions=_counter_service_assumptions(submission),
    )

    for address in submission.addresses:
        body, reason = _address_body(address, vocabularies)
        verification = (
            address_verifications.for_address(submission, address.index)
            if address_verifications
            else None
        )
        usable_llm_address = usable_address_review(verification) if verification else None
        verification_reason = (
            None
            if usable_llm_address
            else verification.action_reason()
            if verification
            else None
        )
        add(
            "address",
            "POST",
            "/courts/{court_id}/v1/address",
            body,
            _combine_reasons(reason, verification_reason),
            source_fields=[f"addresses[{address.index}]"],
            address_verification=(verification.as_dict() if verification else None),
            extra_llm_review_ids=(
                [address_review_id(submission.source.source_row_number, address.index)]
                if usable_llm_address
                else []
            ),
        )

    for contact in submission.contacts:
        body, reason = _contact_body(contact, vocabularies)
        add(
            "contact_detail",
            "POST",
            "/courts/{court_id}/v1/contact-details",
            body,
            reason,
            source_fields=[f"contacts[{contact.index}]"],
        )

    for opening_hours in submission.opening_hours:
        body, reason = _opening_hours_body(opening_hours, vocabularies)
        add(
            "court_opening_hours",
            "PUT",
            "/courts/{court_id}/v1/opening-hours",
            body,
            reason,
            source_fields=[f"opening_hours[{opening_hours.index}]"],
            migration_assumptions=_opening_hours_assumptions(opening_hours),
        )

    actions = _group_collection_actions(actions)
    return FactApiRecord(
        court_slug=submission.court_slug or "unknown-court",
        court_id=court_id,
        source_row_numbers=[submission.source.source_row_number],
        status=submission.status,
        readiness=_record_readiness(actions),
        actions=actions,
    )


_COLLECTION_RESOURCES = {"address", "contact_detail", "court_opening_hours"}


def _group_collection_actions(actions: list[FactApiAction]) -> list[FactApiAction]:
    """Represent a replaceable collection as one logical section action."""

    grouped: list[FactApiAction] = []
    by_resource: dict[str, FactApiAction] = {}
    for action in actions:
        if action.resource not in _COLLECTION_RESOURCES:
            grouped.append(action)
            continue
        existing = by_resource.get(action.resource)
        if existing is None:
            by_resource[action.resource] = action
            grouped.append(action)
            continue
        existing.proposed_items.extend(action.proposed_items or [action.body])
        existing.proposed_item_ids.extend(action.proposed_item_ids)
        existing.proposed_item_source_fields.extend(action.proposed_item_source_fields)
        existing.proposed_item_clear_fields.extend(
            action.proposed_item_clear_fields or [[]]
        )
        existing.source_fields = sorted(set(existing.source_fields + action.source_fields))
        existing.llm_review_ids = sorted(set(existing.llm_review_ids + action.llm_review_ids))
        existing.address_verifications.extend(action.address_verifications)
        existing.request_body_normalisations.update(action.request_body_normalisations)
        existing.migration_assumptions.extend(action.migration_assumptions)
        existing.reason = _combine_reasons(existing.reason, action.reason)
        if action.readiness == "pending":
            existing.readiness = "pending"
    return grouped


def _merge_duplicate_records(records: list[FactApiRecord]) -> list[FactApiRecord]:
    """Defensively retain only the latest row if a caller supplies duplicates."""

    grouped: dict[str, list[FactApiRecord]] = {}
    order: list[str] = []
    for record in records:
        if record.court_slug not in grouped:
            grouped[record.court_slug] = []
            order.append(record.court_slug)
        grouped[record.court_slug].append(record)

    merged: list[FactApiRecord] = []
    for slug in order:
        candidates = grouped[slug]
        if len(candidates) == 1:
            merged.append(candidates[0])
            continue
        merged.append(
            max(
                candidates,
                key=lambda candidate: max(candidate.source_row_numbers or [0]),
            )
        )
    return merged


def _explicit_clear_fields(
    resource: str,
    review_items: list[dict[str, Any]],
    source_fields: list[str],
) -> list[str]:
    if resource != "contact_detail":
        return []
    if any(
        item.get("outcome") == "accepted"
        and item.get("operation") == "clear"
        and any(
            str(item.get("field") or "") == source_field
            or str(item.get("field") or "").startswith(f"{source_field}.")
            for source_field in source_fields
        )
        and str(item.get("field") or "").endswith(".explanation")
        for item in review_items
    ):
        return ["explanation"]
    return []


def _building_facilities_body(facilities: dict[str, Any]) -> dict[str, Any]:
    food_value = facilities.get("food_and_drink")
    food_options = set(food_value or [])
    return _without_none(
        {
            "parking": facilities.get("parking_available"),
            "quietRoom": facilities.get("quiet_room_available_2"),
            "freeWaterDispensers": "Free water dispensers" in food_options
            if food_value is not None
            else None,
            "snackVendingMachines": "Snack vending machines" in food_options
            if food_value is not None
            else None,
            "drinkVendingMachines": "Drink vending machines" in food_options
            if food_value is not None
            else None,
            "cafeteria": "A cafeteria serving hot and cold food" in food_options
            if food_value is not None
            else None,
            "waitingArea": facilities.get("separate_waiting_areas"),
            "waitingAreaChildren": facilities.get("child_waiting_area"),
            "babyChanging": facilities.get("baby_changing"),
            "wifi": facilities.get("wifi_available"),
        }
    )


def _accessibility_options_body(facilities: dict[str, Any]) -> dict[str, Any]:
    hearing_equipment = {
        "Infrared systems and hearing loop systems are available at this court.": (
            "INFRARED_SYSTEMS_AND_HEARING_LOOP_SYSTEMS"
        ),
        "Infrared systems are available at this court.": "INFRARED_SYSTEMS",
        "Hearing loop systems are available at this court.": "HEARING_LOOP_SYSTEMS",
    }
    lift_available = facilities.get("lift_available")
    return _without_none(
        {
            "accessibleParking": facilities.get("accessible_parking"),
            "accessibleParkingPhoneNumber": _valid_phone_or_none(
                facilities.get("accessible_parking_phone")
            ),
            "accessibleToiletDescription": facilities.get("accessible_toilet_description"),
            "accessibleEntrance": facilities.get("accessible_entrance"),
            "accessibleEntrancePhoneNumber": _valid_phone_or_none(
                facilities.get("accessible_entrance_support_phone")
            ),
            "hearingEnhancementEquipment": hearing_equipment.get(
                facilities.get("hearing_enhancement_equipment")
            ),
            "lift": lift_available,
            "liftDoorWidth": _lift_measurement_for_request(
                facilities.get("lift_door_width"),
                lift_available,
                _MIGRATION_DEFAULT_LIFT_DOOR_WIDTH_CM,
                measurement="width",
            ),
            "liftDoorLimit": _lift_measurement_for_request(
                facilities.get("lift_weight_limit"),
                lift_available,
                _MIGRATION_DEFAULT_LIFT_DOOR_LIMIT_KG,
                measurement="weight",
            ),
            "quietRoom": facilities.get("quiet_room_available"),
        }
    )


def _accessibility_options_assumptions(facilities: dict[str, Any]) -> list[str]:
    """Describe approved request-only defaults for incomplete accessibility data."""

    assumptions = []
    if (
        facilities.get("accessible_entrance") is False
        and _is_missing_form_value(facilities.get("accessible_entrance_support_phone"))
    ):
        assumptions.append(
            "Migration policy: accessible entrance is marked unavailable and the source has no "
            "support phone. If live FaCT has no number to preserve, the effective request uses "
            f"{MISSING_SUPPORT_PHONE_PLACEHOLDER}. It does not amend the source or cleaned data."
        )
    if facilities.get("lift_available") is False:
        assumptions.append(
            "Migration policy: lift is marked unavailable and the form has no lift-support phone "
            "field. If live FaCT has no number to preserve, the effective request uses "
            f"{MISSING_SUPPORT_PHONE_PLACEHOLDER}. It does not amend the source or cleaned data."
        )
    if facilities.get("lift_available") is True and _parse_lift_measurement(
        facilities.get("lift_door_width"), "width"
    ) is None:
        assumptions.append(
            "Review-required migration default: lift is marked available but the source has no "
            "usable door width, so this FaCT request uses 1 cm. It does not amend the source or "
            "cleaned data."
        )
    if facilities.get("lift_available") is True and _parse_lift_measurement(
        facilities.get("lift_weight_limit"), "weight"
    ) is None:
        assumptions.append(
            "Review-required migration default: lift is marked available but the source has no "
            "usable weight limit, so this FaCT request uses 1 kg. It does not amend the source or "
            "cleaned data."
        )
    return assumptions


def _translation_body(submission: CourtSubmission) -> dict[str, Any]:
    return _without_none(
        {
            "phoneNumber": _valid_phone_or_none(submission.translation_phone),
            "email": submission.translation_email,
        }
    )


def _professional_information_body(submission: CourtSubmission) -> dict[str, Any]:
    rooms = submission.interview_rooms
    if not _has_professional_information_evidence(rooms):
        return {}

    interview_rooms = rooms.get("has_interview_rooms")

    body = _without_none(
        {
            "interviewRooms": interview_rooms,
            "interviewRoomCount": _interview_room_count_for_request(rooms),
            "interviewPhoneNumber": _valid_phone_or_none(rooms.get("booking_phone")),
            # The Microsoft Forms export does not collect these three fields.
            # Product approved false as the migration default; this applies only
            # to the FaCT request body and never changes the source submission.
            "videoHearings": False,
            "commonPlatform": False,
            "accessScheme": False,
        }
    )
    return {"professionalInformation": body} if body else {}


def _professional_information_assumptions(submission: CourtSubmission) -> list[str]:
    if not _has_professional_information_evidence(submission.interview_rooms):
        return []
    rooms = submission.interview_rooms
    assumptions = [
        "Migration policy: the form does not collect videoHearings, commonPlatform, "
        "or accessScheme, so this request defaults each field to false."
    ]
    if rooms.get("has_interview_rooms") is True and _parse_interview_room_count(
        rooms.get("room_count")
    ) is None:
        assumptions.append(
            "Review-required migration default: interview rooms are marked available but the source has no "
            "usable room count, so this FaCT request uses 1. It does not amend the source or cleaned data."
        )
    elif rooms.get("has_interview_rooms") is False and _positive_int(
        rooms.get("room_count")
    ) not in {
        None,
        0,
    }:
        assumptions.append(
            "Review-required migration default: interview rooms are marked unavailable, so this FaCT request "
            "uses a room count of 0 instead of the contradictory source count. It does not amend the source or cleaned data."
        )
    return assumptions


def _has_professional_information_evidence(rooms: dict[str, Any]) -> bool:
    return any(
        rooms.get(field) is not None
        for field in ("has_interview_rooms", "room_count", "booking_phone")
    )


def _counter_service_body(
    submission: CourtSubmission, vocabularies: Vocabularies | None
) -> tuple[dict[str, Any], str | None]:
    counter = submission.counter_service
    assists_with = set(counter.get("assists_with") or [])
    raw_appointment_contact = counter.get("appointment_contact")
    appointment_contact = (
        raw_appointment_contact
        if isinstance(raw_appointment_contact, str)
        and _FACT_API_EMAIL_PATTERN.fullmatch(raw_appointment_contact)
        else None
    )
    no_counter_service = _has_no_counter_service_status(counter)
    times = [] if no_counter_service else _counter_opening_times(counter)
    evidence = bool(assists_with or appointment_contact or times or no_counter_service)
    if not evidence:
        return {}, None

    court_type_ids, type_reason = _vocabulary_ids(
        counter.get("specific_courts") or [], "court_types", vocabularies
    )
    body = _without_none(
        {
            "counterService": not no_counter_service,
            "assistWithForms": "Forms" in assists_with,
            "assistWithDocuments": "Documents" in assists_with,
            "assistWithSupport": "Support at court" in assists_with,
            "appointmentNeeded": bool(appointment_contact) and not no_counter_service,
            "appointmentContact": appointment_contact if not no_counter_service else None,
            "courtTypes": court_type_ids or None,
            "openingTimesDetails": times or None,
        }
    )
    contradiction = (
        "Counter service is explicitly unavailable but assistance options are also selected"
        if no_counter_service and assists_with
        else None
    )
    return body, _combine_reasons(type_reason, contradiction)


def _has_no_counter_service_status(counter: dict[str, Any]) -> bool:
    return any(
        isinstance(value, (dict, OpeningTime))
        and (
            value.get("status_text") if isinstance(value, dict) else value.status_text
        )
        in _NO_COUNTER_SERVICE_STATUSES
        for value in _counter_time_values(counter)
    )


def _counter_time_values(counter: dict[str, Any]) -> list[Any]:
    return [
        counter.get(field)
        for field in (
            "monday_to_friday",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
        )
    ]


def _counter_service_assumptions(submission: CourtSubmission) -> list[str]:
    counter = submission.counter_service
    assumptions = []
    if _has_no_counter_service_status(counter):
        assumptions.append(
            "The source explicitly states that no counter service is available, so the "
            "request sends counterService=false without appointment details or opening times."
        )
    omitted = sum(
        _counter_time_is_unusable(value) for value in _counter_time_values(counter)
    )
    if omitted and not _has_no_counter_service_status(counter):
        assumptions.append(
            f"Omitted {omitted} unusable counter-service opening period(s). Submitted "
            "counter details remain usable and existing live hours are preserved by merge."
        )
    return assumptions


def _counter_time_is_unusable(value: Any) -> bool:
    if not isinstance(value, (dict, OpeningTime)):
        return False
    status = value.get("status") if isinstance(value, dict) else value.status
    opening = value.get("open") if isinstance(value, dict) else value.open
    closing = value.get("close") if isinstance(value, dict) else value.close
    if status in {"invalid", "known_text_status"}:
        return True
    return bool(
        status == "valid_time"
        and opening
        and closing
        and (opening >= closing or (opening == "00:00" and closing == "00:00"))
    )


def _address_body(address, vocabularies: Vocabularies | None) -> tuple[dict[str, Any], str | None]:
    address_type = {
        "Visit": "VISIT_US",
        "Send documents to": "WRITE_TO_US",
        "Visit and send documents to": "VISIT_OR_CONTACT_US",
    }.get(address.address_type)
    area_ids, area_reason = _vocabulary_ids(address.areas_of_law, "areas_of_law", vocabularies)
    court_type_ids, type_reason = _vocabulary_ids(address.court_types, "court_types", vocabularies)
    reason = _combine_reasons(area_reason, type_reason)
    if address.address_type and address_type is None:
        reason = _combine_reasons(reason, "Address type is not recognised by the FaCT API")
    return (
        _without_none(
            {
                "addressLine1": address.line_1,
                "addressLine2": address.line_2,
                "townCity": address.town_or_city,
                "county": address.county,
                "postcode": address.postcode,
                "addressType": address_type,
                "areasOfLaw": area_ids or None,
                "courtTypes": court_type_ids or None,
            }
        ),
        reason,
    )


def _contact_body(contact, vocabularies: Vocabularies | None) -> tuple[dict[str, Any], str | None]:
    description_id, reason = _vocabulary_id(
        contact.description, "contact_description_types", vocabularies
    )
    return (
        _without_none(
            {
                "courtContactDescriptionId": description_id,
                "explanation": contact.explanation,
                "phoneNumber": _valid_phone_or_none(contact.phone),
                # Keep the source value here so request-only normalisation can
                # record an auditable omission when the optional email is invalid.
                "email": contact.email,
            }
        ),
        reason,
    )


def _opening_hours_body(
    opening_hours, vocabularies: Vocabularies | None
) -> tuple[dict[str, Any], str | None]:
    type_id, reason = _vocabulary_id(opening_hours.type, "opening_hour_types", vocabularies)
    times = _opening_times(opening_hours)
    if not times and _is_no_counter_service_opening_type(opening_hours.type):
        # This value describes the absence of a counter service; it is not a
        # publishable court-opening period. FaCT rejects every opening-hours
        # request without at least one period.
        return {}, None
    if opening_hours.type and type_id is None:
        reason = _combine_reasons(reason, "Opening-hours type does not have a FaCT API UUID")
    if not times:
        reason = _combine_reasons(
            reason,
            "openingTimesDetails must contain at least one valid opening period for this opening-hours type",
        )
    return (
        _without_none(
            {
                "openingHourTypeId": type_id,
                "openingTimesDetails": times or None,
            }
        ),
        reason,
    )


def _is_no_counter_service_opening_type(value: str | None) -> bool:
    return bool(value and value.strip().casefold() in _NO_COUNTER_SERVICE_STATUSES)


def _opening_hours_assumptions(opening_hours: Any) -> list[str]:
    recovered = sum(
        issue.code == "TIME_FORMAT_RECOVERED"
        for value in (
            opening_hours.monday_to_friday,
            opening_hours.monday,
            opening_hours.tuesday,
            opening_hours.wednesday,
            opening_hours.thursday,
            opening_hours.friday,
        )
        if isinstance(value, OpeningTime)
        for issue in value.issues
    )
    if not recovered:
        return []
    return [
        f"Recovered {recovered} unambiguous opening-time value(s) from recognised "
        "formatting noise. Original values remain in immutable audit evidence."
    ]


def _counter_opening_times(counter: dict[str, Any]) -> list[dict[str, str]]:
    if counter.get("same_monday_to_friday") is True:
        details = _time_detail("EVERYDAY", counter.get("monday_to_friday"))
    else:
        details = _weekday_time_details(counter)
    return [
        detail
        for detail in details
        if detail["openingTime"] < detail["closingTime"]
    ]


def _opening_times(opening_hours) -> list[dict[str, str]]:
    if opening_hours.same_monday_to_friday is True:
        return _time_detail("EVERYDAY", opening_hours.monday_to_friday)
    return _weekday_time_details(opening_hours.model_dump())


def _weekday_time_details(values: dict[str, Any]) -> list[dict[str, str]]:
    details = []
    for day, api_day in (
        ("monday", "MONDAY"),
        ("tuesday", "TUESDAY"),
        ("wednesday", "WEDNESDAY"),
        ("thursday", "THURSDAY"),
        ("friday", "FRIDAY"),
    ):
        details.extend(_time_detail(api_day, values.get(day)))
    return details


def _time_detail(day: str, value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        opening_time = value.get("open")
        closing_time = value.get("close")
        status = value.get("status")
    elif isinstance(value, OpeningTime):
        opening_time = value.open
        closing_time = value.close
        status = value.status
    else:
        return []
    if status != "valid_time" or not opening_time or not closing_time:
        return []
    if opening_time == "00:00" and closing_time == "00:00":
        return []
    return [{"dayOfWeek": day, "openingTime": opening_time, "closingTime": closing_time}]


def _vocabulary_ids(
    values: list[str], vocabulary_name: str, vocabularies: Vocabularies | None
) -> tuple[list[str], str | None]:
    ids = []
    reasons = []
    for value in values:
        api_id, reason = _vocabulary_id(value, vocabulary_name, vocabularies)
        if api_id:
            ids.append(api_id)
        if reason:
            reasons.append(reason)
    return ids, "; ".join(reasons) if reasons else None


def _vocabulary_id(
    value: str | None, vocabulary_name: str, vocabularies: Vocabularies | None
) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    entry = vocabularies.normalised_vocab_match(value, vocabulary_name) if vocabularies else None
    if entry is None:
        return None, f"'{value}' is not in the {vocabulary_name} vocabulary"
    if not entry.api_id:
        return None, f"'{entry.name}' does not have a FaCT API UUID"
    return entry.api_id, None


def validate_fact_api_action_body(
    resource: str,
    body: dict[str, Any],
    *,
    require_opening_periods: bool = False,
) -> str | None:
    """Return a human-readable reason when an action cannot satisfy FaCT's API contract.

    The same check is deliberately used while generating a report and immediately
    before execution. The latter protects historic reports generated before a
    contract change from being sent to the API.
    """

    errors = []
    required_fields = {
        "building_facilities": [
            "courtId",
            "parking",
            "freeWaterDispensers",
            "snackVendingMachines",
            "drinkVendingMachines",
            "cafeteria",
            "waitingArea",
            "quietRoom",
            "babyChanging",
            "wifi",
        ],
        "accessibility_options": [
            "courtId",
            "accessibleParking",
            "accessibleEntrance",
            "hearingEnhancementEquipment",
            "lift",
            "quietRoom",
        ],
        "translation_services": ["courtId"],
        "counter_service_opening_hours": [
            "courtId",
            "counterService",
            "assistWithForms",
            "assistWithDocuments",
            "assistWithSupport",
            "appointmentNeeded",
        ],
        "address": ["courtId", "addressLine1", "townCity", "postcode", "addressType"],
        "contact_detail": ["courtId", "courtContactDescriptionId"],
        "court_opening_hours": ["courtId", "openingHourTypeId"],
    }
    for field in required_fields.get(resource, []):
        if body.get(field) is None:
            errors.append(f"{field} is required by the FaCT API")

    if (
        resource == "building_facilities"
        and body.get("waitingArea") is True
        and body.get("waitingAreaChildren") is None
    ):
        errors.append("waitingAreaChildren is required by the FaCT API when waitingArea is true")
    if resource == "accessibility_options":
        for field in (
            "accessibleParkingPhoneNumber",
            "accessibleEntrancePhoneNumber",
            "liftSupportPhoneNumber",
        ):
            phone = body.get(field)
            if phone is not None and (
                not isinstance(phone, str) or not _FACT_API_PHONE_PATTERN.fullmatch(phone)
            ):
                errors.append(f"{field} does not match the FaCT API phone format")
        if body.get("lift") is True:
            for field in ("liftDoorWidth", "liftDoorLimit"):
                if body.get(field) is None:
                    errors.append(f"{field} is required by the FaCT API when lift is true")

    if resource == "address":
        errors.extend(_address_validation_errors(body))
    if resource == "contact_detail":
        errors.extend(_contact_validation_errors(body))
    if resource in {"counter_service_opening_hours", "court_opening_hours"}:
        # Planning may be provisional because merge semantics can supply live
        # periods. The final effective operation must always satisfy FaCT's
        # non-empty list constraint.
        errors.extend(
            _opening_times_validation_errors(
                body, require_periods=require_opening_periods
            )
        )
    if resource == "counter_service_opening_hours":
        appointment_contact = body.get("appointmentContact")
        if body.get("appointmentNeeded") is True and appointment_contact is None:
            errors.append("appointmentContact is required by the FaCT API when appointmentNeeded is true")
        elif appointment_contact is not None and (
            not isinstance(appointment_contact, str)
            or not _FACT_API_EMAIL_PATTERN.fullmatch(appointment_contact)
        ):
            errors.append("appointmentContact does not match the FaCT API email format")

    for field, value in body.items():
        if isinstance(value, str) and len(value) > 255:
            errors.append(f"{field} exceeds the API maximum length")
    if resource == "accessibility_options":
        description = body.get("accessibleToiletDescription")
        if description and not re.fullmatch(r"[A-Za-z0-9 ()':,\-;.]+", description):
            errors.append(
                "accessibleToiletDescription contains characters rejected by the FaCT API"
            )
    if resource == "contact_detail":
        explanation = body.get("explanation")
        if explanation and len(explanation) > 250:
            errors.append("explanation exceeds the API maximum length")
        if explanation and not re.fullmatch(r"[A-Za-z0-9 '\-()&+]*", explanation):
            errors.append("explanation contains characters rejected by the FaCT API")
    if resource == "professional_information":
        professional_information = body.get("professionalInformation")
        if not isinstance(professional_information, dict):
            errors.append("professionalInformation is required by the FaCT API")
        else:
            for field in ("interviewRooms", "videoHearings", "commonPlatform", "accessScheme"):
                if professional_information.get(field) is None:
                    errors.append(f"professionalInformation.{field} is required by the FaCT API")
            interview_rooms = professional_information.get("interviewRooms")
            room_count = professional_information.get("interviewRoomCount")
            if interview_rooms is True and (
                not isinstance(room_count, int) or not 1 <= room_count <= 150
            ):
                errors.append(
                    "professionalInformation.interviewRoomCount must be between 1 and 150 "
                    "when interviewRooms is true"
                )
            if interview_rooms is False and room_count is not None and room_count != 0:
                errors.append(
                    "professionalInformation.interviewRoomCount must be omitted or zero "
                    "when interviewRooms is false"
                )
    return "; ".join(errors) if errors else None


def normalise_fact_api_action_body(resource: str, body: dict[str, Any]) -> dict[str, Any]:
    """Apply conservative request-only normalisation required by FaCT.

    Source data remains untouched.  Changes are restricted to conventional
    equivalents and formatting that the FaCT request validators reject, such
    as typographic punctuation, ``C/o`` and ampersands.
    """

    cleaned = dict(body)
    if resource == "address":
        for field in ("addressLine1", "addressLine2", "townCity", "county"):
            value = cleaned.get(field)
            if isinstance(value, str):
                cleaned[field] = _normalise_fact_api_address_text(value)
    elif resource == "accessibility_options":
        value = cleaned.get("accessibleToiletDescription")
        if isinstance(value, str):
            cleaned["accessibleToiletDescription"] = _normalise_fact_api_public_text(value)
    elif resource == "contact_detail":
        value = cleaned.get("explanation")
        if isinstance(value, str):
            cleaned["explanation"] = _normalise_fact_api_explanation(value)
        email = cleaned.get("email")
        if email is not None and (
            not isinstance(email, str) or not _FACT_API_EMAIL_PATTERN.fullmatch(email)
        ):
            cleaned.pop("email", None)
    elif resource == "counter_service_opening_hours":
        appointment_contact = cleaned.get("appointmentContact")
        if appointment_contact is not None and (
            not isinstance(appointment_contact, str)
            or not _FACT_API_EMAIL_PATTERN.fullmatch(appointment_contact)
        ):
            cleaned.pop("appointmentContact", None)
            cleaned["appointmentNeeded"] = False
    return cleaned


def _normalise_fact_api_address_text(value: str) -> str:
    value = _normalise_fact_api_public_text(value)
    return re.sub(r"\s+", " ", value).strip()


def _normalise_fact_api_public_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(
        str.maketrans(
            {
                "\u2018": "'",
                "\u2019": "'",
                "\u201c": "'",
                "\u201d": "'",
                "\u2013": "-",
                "\u2014": "-",
                "\u00a0": " ",
            }
        )
    )
    value = _ADDRESS_CARE_OF_PATTERN.sub("care of", value)
    value = re.sub(r"\bN\s*/\s*A\b", "Not applicable", value, flags=re.IGNORECASE)
    value = value.replace("|", " - ")
    return value.replace("&", " and ")


def _normalise_fact_api_explanation(value: str) -> str:
    """Convert harmless display punctuation into the strict contact API charset."""

    value = _normalise_fact_api_public_text(value)
    # The contact explanation API accepts words, spaces, apostrophes, hyphens,
    # brackets, ampersands and plus signs.  Sentence separators carry no data
    # in this one-line description field, so replace rather than silently drop.
    value = re.sub(r"[,:;./]+", " ", value)
    # Replace any remaining disallowed punctuation with a separator rather
    # than silently joining neighbouring words. The field is an optional short
    # explanation; this preserves readable prose without inventing content.
    value = re.sub(r"[^A-Za-z0-9 '\-()&+]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _body_normalisations(
    original: dict[str, Any], cleaned: dict[str, Any]
) -> dict[str, dict[str, str]]:
    changes: dict[str, dict[str, str]] = {}
    for field, original_value in original.items():
        cleaned_value = cleaned.get(field)
        if isinstance(original_value, str) and field not in cleaned:
            changes[field] = {
                "from": original_value,
                "to": "Omitted because the optional value is invalid",
            }
            continue
        if (
            isinstance(original_value, str)
            and isinstance(cleaned_value, str)
            and original_value != cleaned_value
        ):
            changes[field] = {"from": original_value, "to": cleaned_value}
    return changes


def _empty_contact_item_without_clear(
    resource: str,
    body: dict[str, Any],
    explicit_clear_fields: list[str],
) -> bool:
    if resource != "contact_detail":
        return False
    return not explicit_clear_fields and not any(
        body.get(field) for field in ("phoneNumber", "email", "explanation")
    )


def _address_validation_errors(body: dict[str, Any]) -> list[str]:
    errors = []
    for field in ("addressLine1", "addressLine2", "townCity", "county"):
        value = body.get(field)
        if value is not None and (
            not isinstance(value, str) or not _FACT_API_ADDRESS_PATTERN.fullmatch(value)
        ):
            errors.append(f"{field} contains characters rejected by the FaCT API")

    for field, maximum in (("townCity", 100), ("county", 100)):
        value = body.get(field)
        if isinstance(value, str) and len(value) > maximum:
            errors.append(f"{field} exceeds the API maximum length")

    postcode = body.get("postcode")
    if isinstance(postcode, str):
        postcode_reason = _fact_api_postcode_reason(postcode)
        if postcode_reason:
            errors.append(postcode_reason)
    return errors


def _fact_api_postcode_reason(postcode: str) -> str | None:
    compact = re.sub(r"\s+", "", postcode).upper()
    if compact.startswith("BT"):
        return "postcode is in Northern Ireland, which the FaCT API does not support"
    if _FACT_API_CI_IOM_POSTCODE_PATTERN.match(compact):
        return (
            "postcode is in a Channel Islands or Isle of Man region the FaCT API does not support"
        )
    if " " not in postcode.strip():
        return "postcode must contain a space for the FaCT/Ordnance Survey lookup"
    return None


def _contact_validation_errors(body: dict[str, Any]) -> list[str]:
    errors = []
    phone = body.get("phoneNumber")
    if phone is not None and (
        not isinstance(phone, str) or not _FACT_API_PHONE_PATTERN.fullmatch(phone)
    ):
        errors.append("phoneNumber does not match the FaCT API phone format")
    email = body.get("email")
    if email is not None and (
        not isinstance(email, str) or not _FACT_API_EMAIL_PATTERN.fullmatch(email)
    ):
        errors.append("email does not match the FaCT API email format")
    return errors


def _opening_times_validation_errors(
    body: dict[str, Any], *, require_periods: bool = True
) -> list[str]:
    details = body.get("openingTimesDetails")
    if not isinstance(details, list) or not details:
        return (
            ["openingTimesDetails must contain at least one valid opening period for the FaCT API"]
            if require_periods
            else []
        )

    errors = []
    seen_days = set()
    every_day = False
    for detail in details:
        if not isinstance(detail, dict):
            errors.append("openingTimesDetails contains an invalid opening period")
            continue
        day = detail.get("dayOfWeek")
        opening = detail.get("openingTime")
        closing = detail.get("closingTime")
        if day not in _FACT_API_OPENING_DAYS:
            errors.append("openingTimesDetails contains an invalid day of week")
        elif day in seen_days:
            errors.append("openingTimesDetails contains a duplicate day of week")
        else:
            seen_days.add(day)
            every_day = every_day or day == "EVERYDAY"

        if not isinstance(opening, str) or not _FACT_API_TIME_PATTERN.fullmatch(opening):
            errors.append("openingTimesDetails contains an invalid opening time")
        if not isinstance(closing, str) or not _FACT_API_TIME_PATTERN.fullmatch(closing):
            errors.append("openingTimesDetails contains an invalid closing time")
        if (
            isinstance(opening, str)
            and isinstance(closing, str)
            and _FACT_API_TIME_PATTERN.fullmatch(opening)
            and _FACT_API_TIME_PATTERN.fullmatch(closing)
            and opening >= closing
        ):
            errors.append(
                "openingTimesDetails requires each opening time to be before its closing time"
            )

    if every_day and len(details) != 1:
        errors.append("openingTimesDetails may only contain EVERYDAY as its sole day")
    return errors


def _record_readiness(
    actions: list[FactApiAction],
) -> Literal["ready", "partially_ready", "pending", "not_applicable"]:
    if not actions:
        return "not_applicable"
    ready = sum(action.readiness == "ready" for action in actions)
    if ready == len(actions):
        return "ready"
    if ready:
        return "partially_ready"
    return "pending"


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _is_missing_form_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def is_unavailable_lift_measurement(value: Any) -> bool:
    """Return whether a submitted lift measurement needs the approved minimum."""

    return _parse_lift_measurement(value, "width") is None and _parse_lift_measurement(
        value, "weight"
    ) is None


def _lift_measurement_for_request(
    value: Any,
    lift_available: Any,
    migration_default: int,
    *,
    measurement: Literal["width", "weight"],
) -> int | None:
    """Normalise explicit units or use a placeholder for a blank dependent answer.

    FaCT expects door width as an integer number of centimetres and lift limit
    as an integer number of kilograms.  Forms commonly include those units in
    the answer (for example ``800 mm`` or ``650KG``), so retain unambiguous
    measurements rather than treating them as absent.  Explicit invalid or
    ambiguous text remains blocked.  The migration default is reserved for a
    genuinely blank dependent answer when the lift answer is yes.
    """

    parsed = _parse_lift_measurement(value, measurement)
    if parsed is not None:
        return parsed
    if lift_available is True:
        return migration_default
    return None


def _parse_lift_measurement(
    value: Any, measurement: Literal["width", "weight"]
) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return _positive_integral_decimal(value)
    if not isinstance(value, str):
        return None

    text = unicodedata.normalize("NFKC", value).strip().casefold()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return _positive_integral_decimal(text)

    if measurement == "width":
        units = {
            "mm": Decimal("0.1"),
            "millimetre": Decimal("0.1"),
            "millimetres": Decimal("0.1"),
            "millimeter": Decimal("0.1"),
            "millimeters": Decimal("0.1"),
            "cm": Decimal("1"),
            "cms": Decimal("1"),
            "centimetre": Decimal("1"),
            "centimetres": Decimal("1"),
            "centimeter": Decimal("1"),
            "centimeters": Decimal("1"),
            "m": Decimal("100"),
            "metre": Decimal("100"),
            "metres": Decimal("100"),
            "meter": Decimal("100"),
            "meters": Decimal("100"),
            "in": Decimal("2.54"),
            "inch": Decimal("2.54"),
            "inches": Decimal("2.54"),
            "ft": Decimal("30.48"),
            "foot": Decimal("30.48"),
            "feet": Decimal("30.48"),
        }
        unit_pattern = (
            r"millimetres?|millimeters?|centimetres?|centimeters?|cms?|mm|"
            r"metres?|meters?|inches?|in|feet|foot|ft|m"
        )
    else:
        units = {
            "kg": Decimal("1"),
            "kgs": Decimal("1"),
            "kilogram": Decimal("1"),
            "kilograms": Decimal("1"),
            "lb": Decimal("0.45359237"),
            "lbs": Decimal("0.45359237"),
            "pound": Decimal("0.45359237"),
            "pounds": Decimal("0.45359237"),
        }
        unit_pattern = r"kilograms?|kgs?|pounds?|lbs?"

    converted = []
    for match in re.finditer(rf"(?<![\d.])(\d+(?:\.\d+)?)\s*({unit_pattern})\b", text):
        number = _decimal(match.group(1))
        if number is not None:
            converted.append(number * units[match.group(2)])
    if not converted:
        return None
    parsed_values = [int(item) for item in converted if item >= 1]
    return min(parsed_values) if parsed_values else None


def _positive_integral_decimal(value: Any) -> int | None:
    number = _decimal(value)
    if (
        number is None
        or not number.is_finite()
        or number <= 0
        or number != number.to_integral_value()
    ):
        return None
    return int(number)


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _interview_room_count_for_request(rooms: dict[str, Any]) -> int | None:
    """Represent an explicit rooms yes/no answer without changing source evidence."""

    has_interview_rooms = rooms.get("has_interview_rooms")
    value = rooms.get("room_count")
    if has_interview_rooms is False:
        return 0

    parsed = _parse_interview_room_count(value)
    if parsed is not None:
        return parsed
    if has_interview_rooms is True:
        return _MIGRATION_DEFAULT_INTERVIEW_ROOM_COUNT
    return None


_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_COUNT_TOKEN = r"(?:\d+|" + "|".join(_NUMBER_WORDS) + r")"


def _parse_interview_room_count(value: Any) -> int | None:
    direct = _positive_int(value)
    if direct is not None:
        return direct if direct <= 150 else None
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    if not text:
        return None

    def number(token: str) -> int:
        return int(token) if token.isdigit() else _NUMBER_WORDS[token]

    room_matches = re.findall(
        rf"\b({_COUNT_TOKEN})\b(?=[^.;]{{0,30}}\b(?:interview|consultation|small)?\s*rooms?\b)",
        text,
    )
    if room_matches:
        total = sum(number(token) for token in room_matches)
        return total if 1 <= total <= 150 else None
    whole_word = _NUMBER_WORDS.get(text)
    if whole_word is not None:
        return whole_word
    leading = re.match(rf"^(?:at\s+least\s+)?({_COUNT_TOKEN})\b", text)
    if leading:
        parsed = number(leading.group(1))
        return parsed if parsed <= 150 else None
    return None


def _valid_phone_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    phone = re.sub(r"\s+", " ", value).strip()
    return phone if _FACT_API_PHONE_PATTERN.fullmatch(phone) else None


def _proposed_item_id(source_row: int, resource: str, source_fields: list[str]) -> str:
    source = ",".join(sorted(source_fields)) or resource
    safe_source = re.sub(r"[^a-z0-9]+", "-", source.casefold()).strip("-")
    return f"row-{source_row}-{resource}-{safe_source}"


def _combine_reasons(*reasons: str | None) -> str | None:
    values = [reason for reason in reasons if reason]
    return "; ".join(values) if values else None
