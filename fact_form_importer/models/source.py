"""Source spreadsheet metadata models."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class SourceMetadata(BaseModel):
    source_row_number: int
    forms_id: Optional[str] = None
    start_time: Optional[str] = None
    completion_time: Optional[str] = None
    submitter_email: Optional[str] = None
    submitter_name: Optional[str] = None
    last_modified_time: Optional[str] = None
