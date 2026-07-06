"""Models for grouped FaCT import records."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from fact_form_importer.models.court_submission import Address, ContactDetail, OpeningHoursSet
from fact_form_importer.models.issues import Issue


class CourtImportRecord(BaseModel):
    court_slug: str
    source_row_numbers: list[int] = Field(default_factory=list)
    addresses: list[Address] = Field(default_factory=list)
    contacts: list[ContactDetail] = Field(default_factory=list)
    opening_hours: list[OpeningHoursSet] = Field(default_factory=list)
    facilities: dict[str, Any] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)
    status: Literal[
        "processed",
        "processed_with_warnings",
        "needs_human_review",
        "failed",
    ] = "processed"
