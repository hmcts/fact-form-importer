"""Schemas for LLM-assisted field normalisation."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


Confidence = Literal["high", "medium", "low"]
IssueSeverity = Literal["info", "warning", "error"]


class LlmField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    raw_value: Any
    cleaned_value: Any = None


class LlmNormalisationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    source_row_number: int
    court_slug: Optional[str] = None
    fields: list[LlmField]
    allowed_vocabularies: dict[str, list[str]] = Field(default_factory=dict)
    field_rules: dict[str, list[str]] = Field(default_factory=dict)


class LlmIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    code: str
    severity: IssueSeverity
    message: str


class LlmNormalisedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    value: Optional[str]
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
