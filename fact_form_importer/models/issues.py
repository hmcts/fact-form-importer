"""Issue models used during import processing."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel


class Issue(BaseModel):
    field: str
    code: str
    severity: Literal["info", "warning", "error"]
    message: str
    raw_value: Optional[Any] = None
    cleaned_value: Optional[Any] = None
