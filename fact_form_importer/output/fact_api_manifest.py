"""Generate an immutable, reviewable endpoint plan for guarded FaCT execution.

This module only generates actions. The separate execution service rechecks the
live court and target section before it may send any POST or PUT request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from fact_form_importer.models.court_submission import CourtSubmission, OpeningTime
from fact_form_importer.validators.fact_api_courts import CourtReference
from fact_form_importer.validators.vocabularies import Vocabularies

IMPORTABLE_STATUSES = {"processed", "processed_with_warnings"}
API_MANIFEST_VERSION = "1.1"

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
) -> FactApiManifestResult:
    """Build an action plan from importable submissions without executing it."""

    records = []
    for submission in submissions:
        if not _is_importable(submission):
            continue
        records.append(_build_record(submission, vocabularies, court_lookup))

    actions = [action for record in records for action in record.actions]
    summary = {
        "api_manifest_record_count": len(records),
        "api_manifest_ready_action_count": sum(action.readiness == "ready" for action in actions),
        "api_manifest_pending_action_count": sum(action.readiness == "pending" for action in actions),
        "api_manifest_ready_record_count": sum(record.readiness == "ready" for record in records),
        "api_manifest_partially_ready_record_count": sum(
            record.readiness == "partially_ready" for record in records
        ),
        "api_manifest_pending_record_count": sum(record.readiness == "pending" for record in records),
    }
    return FactApiManifestResult(
        manifest=FactApiImportManifest(run_id=run_id, records=records, summary=summary)
    )


def _is_importable(submission: CourtSubmission) -> bool:
    return submission.status in IMPORTABLE_STATUSES and not any(
        issue.severity == "error" for issue in submission.issues
    )


def _build_record(
    submission: CourtSubmission,
    vocabularies: Vocabularies | None,
    court_lookup: CourtLookup | None,
) -> FactApiRecord:
    court_reference = court_lookup(submission.court_slug) if court_lookup and submission.court_slug else None
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
    ) -> None:
        nonlocal action_number
        # FaCT validates the request object before its service layer applies the
        # court UUID from the path, so all entity request bodies must carry it.
        # Professional information uses a DTO rather than an entity and does
        # not expose a courtId field.
        if court_id and resource != "professional_information" and body:
            body = {"courtId": court_id, **body}
        if not body:
            return
        action_reason = reason
        if court_id is None:
            action_reason = _combine_reasons(action_reason, "FaCT court UUID could not be resolved")
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
    add(
        "accessibility_options",
        "POST",
        "/courts/{court_id}/v1/accessibility-options",
        _accessibility_options_body(submission.facilities),
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
        _professional_information_reason(submission),
        source_fields=["interview_rooms"],
    )

    counter_body, counter_reason = _counter_service_body(submission, vocabularies)
    add(
        "counter_service_opening_hours",
        "PUT",
        "/courts/{court_id}/v1/opening-hours/counter-service",
        counter_body,
        counter_reason,
        source_fields=["counter_service"],
    )

    for address in submission.addresses:
        body, reason = _address_body(address, vocabularies)
        add(
            "address",
            "POST",
            "/courts/{court_id}/v1/address",
            body,
            reason,
            source_fields=[f"addresses[{address.index}]"],
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
        )

    return FactApiRecord(
        court_slug=submission.court_slug or "unknown-court",
        court_id=court_id,
        source_row_numbers=[submission.source.source_row_number],
        status=submission.status,
        readiness=_record_readiness(actions),
        actions=actions,
    )


def _building_facilities_body(facilities: dict[str, Any]) -> dict[str, Any]:
    food_value = facilities.get("food_and_drink")
    food_options = set(food_value or [])
    return _without_none(
        {
            "parking": facilities.get("parking_available"),
            "quietRoom": facilities.get("quiet_room_available_2"),
            "freeWaterDispensers": "Free water dispensers" in food_options if food_value is not None else None,
            "snackVendingMachines": "Snack vending machines" in food_options if food_value is not None else None,
            "drinkVendingMachines": "Drink vending machines" in food_options if food_value is not None else None,
            "cafeteria": "A cafeteria serving hot and cold food" in food_options if food_value is not None else None,
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
    return _without_none(
        {
            "accessibleParking": facilities.get("accessible_parking"),
            "accessibleParkingPhoneNumber": facilities.get("accessible_parking_phone"),
            "accessibleToiletDescription": facilities.get("accessible_toilet_description"),
            "accessibleEntrance": facilities.get("accessible_entrance"),
            "accessibleEntrancePhoneNumber": facilities.get("accessible_entrance_support_phone"),
            "hearingEnhancementEquipment": hearing_equipment.get(
                facilities.get("hearing_enhancement_equipment")
            ),
            "lift": facilities.get("lift_available"),
            "liftDoorWidth": _positive_int(facilities.get("lift_door_width")),
            "liftDoorLimit": _positive_int(facilities.get("lift_weight_limit")),
            "quietRoom": facilities.get("quiet_room_available"),
        }
    )


def _translation_body(submission: CourtSubmission) -> dict[str, Any]:
    return _without_none(
        {
            "phoneNumber": submission.translation_phone,
            "email": submission.translation_email,
        }
    )


def _professional_information_body(submission: CourtSubmission) -> dict[str, Any]:
    rooms = submission.interview_rooms
    body = _without_none(
        {
            "interviewRooms": rooms.get("has_interview_rooms"),
            "interviewRoomCount": _positive_int(rooms.get("room_count")),
            "interviewPhoneNumber": rooms.get("booking_phone"),
        }
    )
    return {"professionalInformation": body} if body else {}


def _professional_information_reason(submission: CourtSubmission) -> str | None:
    """The form does not collect all required ProfessionalInformationDto values."""

    if not submission.interview_rooms:
        return None
    return (
        "The form does not collect the FaCT-required videoHearings, commonPlatform, "
        "and accessScheme values for professional information"
    )


def _counter_service_body(
    submission: CourtSubmission, vocabularies: Vocabularies | None
) -> tuple[dict[str, Any], str | None]:
    counter = submission.counter_service
    assists_with = set(counter.get("assists_with") or [])
    appointment_contact = counter.get("appointment_contact")
    times = _counter_opening_times(counter)
    evidence = bool(assists_with or appointment_contact or times)
    if not evidence:
        return {}, None

    court_type_ids, type_reason = _vocabulary_ids(
        counter.get("specific_courts") or [], "court_types", vocabularies
    )
    body = _without_none(
        {
            "counterService": True,
            "assistWithForms": "Forms" in assists_with,
            "assistWithDocuments": "Documents" in assists_with,
            "assistWithSupport": "Support at court" in assists_with,
            "appointmentNeeded": bool(appointment_contact),
            "appointmentContact": appointment_contact,
            "courtTypes": court_type_ids or None,
            "openingTimesDetails": times or None,
        }
    )
    return body, type_reason


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
                "phoneNumber": contact.phone,
                "email": contact.email,
            }
        ),
        reason,
    )


def _opening_hours_body(opening_hours, vocabularies: Vocabularies | None) -> tuple[dict[str, Any], str | None]:
    type_id, reason = _vocabulary_id(opening_hours.type, "opening_hour_types", vocabularies)
    times = _opening_times(opening_hours)
    if opening_hours.type and type_id is None:
        reason = _combine_reasons(reason, "Opening-hours type does not have a FaCT API UUID")
    return (
        _without_none(
            {
                "openingHourTypeId": type_id,
                "openingTimesDetails": times or None,
            }
        ),
        reason,
    )


def _counter_opening_times(counter: dict[str, Any]) -> list[dict[str, str]]:
    if counter.get("same_monday_to_friday") is True:
        return _time_detail("EVERYDAY", counter.get("monday_to_friday"))
    return _weekday_time_details(counter)


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


def validate_fact_api_action_body(resource: str, body: dict[str, Any]) -> str | None:
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

    if resource == "building_facilities" and body.get("waitingArea") is True and body.get("waitingAreaChildren") is None:
        errors.append("waitingAreaChildren is required by the FaCT API when waitingArea is true")
    if resource == "accessibility_options":
        if body.get("accessibleEntrance") is False and not body.get("accessibleEntrancePhoneNumber"):
            errors.append(
                "accessibleEntrancePhoneNumber is required by the FaCT API when accessibleEntrance is false"
            )
        if body.get("lift") is True:
            for field in ("liftDoorWidth", "liftDoorLimit"):
                if body.get(field) is None:
                    errors.append(f"{field} is required by the FaCT API when lift is true")
        if body.get("lift") is False and not body.get("liftSupportPhoneNumber"):
            errors.append(
                "liftSupportPhoneNumber is required by the FaCT API when lift is false; "
                "the form does not collect this value"
            )
    for field, value in body.items():
        if isinstance(value, str) and len(value) > 255:
            errors.append(f"{field} exceeds the API maximum length")
    if resource == "accessibility_options":
        description = body.get("accessibleToiletDescription")
        if description and not re.fullmatch(r"[A-Za-z0-9 ()':,\-;.]+", description):
            errors.append("accessibleToiletDescription contains characters rejected by the FaCT API")
    if resource == "contact_detail":
        explanation = body.get("explanation")
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
    return "; ".join(errors) if errors else None


def _record_readiness(actions: list[FactApiAction]) -> Literal[
    "ready", "partially_ready", "pending", "not_applicable"
]:
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


def _combine_reasons(*reasons: str | None) -> str | None:
    values = [reason for reason in reasons if reason]
    return "; ".join(values) if values else None
