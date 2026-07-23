"""Models for one Microsoft Forms court submission row."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from fact_form_importer.models.issues import Issue
from fact_form_importer.models.source import SourceMetadata


class Address(BaseModel):
    index: int
    address_type: Optional[str] = None
    line_1: Optional[str] = None
    line_2: Optional[str] = None
    town_or_city: Optional[str] = None
    county: Optional[str] = None
    postcode: Optional[str] = None
    areas_of_law: list[str] = Field(default_factory=list)
    court_types: list[str] = Field(default_factory=list)


class ContactDetail(BaseModel):
    index: int
    description: Optional[str] = None
    explanation: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class OpeningTime(BaseModel):
    open: Optional[str] = None
    close: Optional[str] = None
    status: Optional[str] = None
    status_text: Optional[str] = None
    issues: list[Issue] = Field(default_factory=list)


class OpeningHoursSet(BaseModel):
    index: int
    type: Optional[str] = None
    same_monday_to_friday: Optional[bool] = None
    monday_to_friday: Optional[OpeningTime] = None
    monday: Optional[OpeningTime] = None
    tuesday: Optional[OpeningTime] = None
    wednesday: Optional[OpeningTime] = None
    thursday: Optional[OpeningTime] = None
    friday: Optional[OpeningTime] = None


class CourtSubmission(BaseModel):
    source: SourceMetadata
    court_slug_raw: Optional[str] = None
    court_slug: Optional[str] = None
    facilities: dict[str, Any] = Field(default_factory=dict)
    translation_phone: Optional[str] = None
    translation_email: Optional[str] = None
    addresses: list[Address] = Field(default_factory=list)
    counter_service: dict[str, Any] = Field(default_factory=dict)
    interview_rooms: dict[str, Any] = Field(default_factory=dict)
    contacts: list[ContactDetail] = Field(default_factory=list)
    opening_hours: list[OpeningHoursSet] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    cleaned: dict[str, Any] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)
    selection_status: Literal["authoritative", "superseded"] = "authoritative"
    superseded_by_source_row_number: Optional[int] = None
    status: Literal[
        "unprocessed",
        "processed",
        "processed_with_warnings",
        "needs_human_review",
        "failed",
        "skipped",
    ] = "unprocessed"


def sync_cleaned_snapshot(submission: CourtSubmission) -> None:
    """Refresh the inspectable cleaned-value snapshot after a safe mutation.

    Import stages retain the original spreadsheet values in ``raw``.  This
    helper keeps the separately exported cleaned snapshot consistent when a
    later deterministic or LLM-assisted stage updates the structured model.
    """

    submission.cleaned = {
        "court_slug": submission.court_slug,
        "facilities": submission.facilities,
        "translation_phone": submission.translation_phone,
        "translation_email": submission.translation_email,
        "addresses": [address.model_dump(mode="json") for address in submission.addresses],
        "counter_service": submission.counter_service,
        "interview_rooms": submission.interview_rooms,
        "contacts": [contact.model_dump(mode="json") for contact in submission.contacts],
        "opening_hours": [hours.model_dump(mode="json") for hours in submission.opening_hours],
    }
