"""Microsoft Forms multiselect cleaning helpers."""

from __future__ import annotations

import re

from fact_form_importer.cleaners.strings import EMPTY_LIKE_STRINGS, null_if_empty_like, trim


def split_multiselect(value: object) -> list[str]:
    cleaned = trim(value)
    if cleaned is None:
        return []

    if cleaned.lower() in EMPTY_LIKE_STRINGS:
        return []

    if ";" in cleaned:
        parts = cleaned.split(";")
    else:
        parts = re.split(r"\n+", cleaned)

    return [part.strip() for part in parts if null_if_empty_like(part) is not None]
