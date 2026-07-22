"""Serializable state for FaCT API execution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ActionExecutionStatus = Literal[
    "planned",
    "awaiting_approval",
    "ready",
    "blocked",
    "running",
    "succeeded",
    "failed",
    "unknown",
]
CourtExecutionStatus = Literal[
    "not_started", "awaiting_approval", "in_progress", "attention_required", "completed"
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ActionAttempt(BaseModel):
    at: str = Field(default_factory=utc_now)
    operation: Literal["preflight", "execute"]
    outcome: ActionExecutionStatus
    http_status: Optional[int] = None
    message: Optional[str] = None
    request_duration_ms: Optional[float] = None
    persistence_duration_ms: Optional[float] = None
    write_request_count: Optional[int] = None
    accepted_write_count: Optional[int] = None
    rejected_write_count: Optional[int] = None
    unknown_write_count: Optional[int] = None


class ActionExecutionState(BaseModel):
    action_id: str
    status: ActionExecutionStatus = "planned"
    attempts: list[ActionAttempt] = Field(default_factory=list)
    last_checked_at: Optional[str] = None
    last_response_status: Optional[int] = None
    reason: Optional[str] = None


class CourtExecutionState(BaseModel):
    # Old local ledgers can contain the removed importer-approval fields.
    model_config = ConfigDict(extra="ignore")

    court_slug: str
    court_id: Optional[str] = None
    status: CourtExecutionStatus = "not_started"
    actions: dict[str, ActionExecutionState] = Field(default_factory=dict)


class ExecutionLedger(BaseModel):
    ledger_version: str = "1.0"
    run_id: str
    updated_at: str = Field(default_factory=utc_now)
    courts: dict[str, CourtExecutionState] = Field(default_factory=dict)
