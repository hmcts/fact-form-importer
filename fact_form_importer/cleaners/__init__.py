"""Deterministic cleaner functions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from fact_form_importer.models.issues import Issue


@dataclass
class CleaningResult:
    value: Optional[Any]
    issues: list[Issue] = field(default_factory=list)
    status: Optional[str] = None
