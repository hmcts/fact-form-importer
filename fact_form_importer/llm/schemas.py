"""Schemas for LLM-assisted field normalisation."""

from __future__ import annotations

from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


Confidence = Literal["high", "medium", "low"]
IssueSeverity = Literal["info", "warning", "error"]


class LlmField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    raw_value: Any
    cleaned_value: Any = None


class LlmAddressCandidate(BaseModel):
    """One OS candidate the model may select by its supplied UPRN only."""

    model_config = ConfigDict(extra="forbid")

    uprn: str
    organisation_name: Optional[str] = None
    building_number: Optional[str] = None
    building_name: Optional[str] = None
    thoroughfare_name: Optional[str] = None
    post_town: Optional[str] = None


class LlmAddressCandidateRequest(BaseModel):
    """A minimal unresolved-address comparison request.

    Postcodes, contact data, metadata and court identifiers are deliberately
    absent. Every candidate already came from the same deterministic postcode
    lookup.
    """

    model_config = ConfigDict(extra="forbid")

    address_index: int
    submitted_address: dict[str, Optional[str]]
    candidates: list[LlmAddressCandidate]


class LlmNormalisationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    source_row_number: int
    court_slug: Optional[str] = None
    fields: list[LlmField]
    allowed_vocabularies: dict[str, list[str]] = Field(default_factory=dict)
    field_rules: dict[str, list[str]] = Field(default_factory=dict)
    address_candidates: list[LlmAddressCandidateRequest] = Field(default_factory=list)


class LlmIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    code: str
    severity: IssueSeverity
    message: str


class LlmNormalisedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    # Some approved fields are Microsoft Forms multi-select values, so an LLM
    # may safely return either one canonical value or a canonical value list.
    value: Optional[Union[str, List[str]]]
    confidence: Confidence
    needs_human_review: bool
    reason: str


class LlmAddressMatch(BaseModel):
    """An advisory selection from a supplied OS candidate list."""

    model_config = ConfigDict(extra="forbid")

    address_index: int
    # Azure structured output requires every property to be listed in
    # ``required``. A nullable-but-required key lets the model explicitly
    # return ``null`` when no supplied candidate is a safe match.
    uprn: Optional[str]
    confidence: Confidence
    needs_human_review: bool
    reason: str


class LlmNormalisationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    normalised_fields: list[LlmNormalisedField]
    confidence: Confidence
    needs_human_review: bool
    issues: list[LlmIssue]
    # As above, return an empty list rather than omitting this key. This keeps
    # the generated JSON schema compatible with Azure structured outputs.
    address_matches: list[LlmAddressMatch]
