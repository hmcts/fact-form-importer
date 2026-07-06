"""String cleaning helpers."""

from __future__ import annotations

import math
import re
from typing import Any, Optional

EMPTY_LIKE_STRINGS = {"", "n/a", "na", "-", "."}


def trim(value: Any) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, float) and math.isnan(value):
        return None

    return str(value).strip()


def collapse_spaces(value: Any) -> Optional[str]:
    trimmed = trim(value)
    if trimmed is None:
        return None

    return re.sub(r"\s+", " ", trimmed)


def null_if_empty_like(value: Any) -> Optional[str]:
    collapsed = collapse_spaces(value)
    if collapsed is None:
        return None

    if collapsed.lower() in EMPTY_LIKE_STRINGS:
        return None

    return collapsed
