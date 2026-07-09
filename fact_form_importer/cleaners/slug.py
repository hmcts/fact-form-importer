"""Court slug cleaning helpers."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from fact_form_importer.cleaners.strings import null_if_empty_like


def normalise_court_slug(value: object) -> str | None:
    cleaned = null_if_empty_like(value)
    if cleaned is None:
        return None

    candidate = cleaned.strip()
    if candidate.lower().startswith("ttps://"):
        candidate = "h" + candidate

    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        path_parts = [part for part in parsed.path.split("/") if part]
        if "courts" in path_parts:
            court_index = path_parts.index("courts")
            if court_index + 1 < len(path_parts):
                candidate = path_parts[court_index + 1]
        elif path_parts:
            candidate = path_parts[-1]
        elif "." not in parsed.netloc:
            candidate = parsed.netloc

    candidate = re.sub(r"^[./\\]+", "", candidate)
    candidate = re.sub(r"^courts[/\\]+", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    candidate = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", candidate)
    candidate = candidate.strip().lower()
    candidate = re.sub(r"[\s_]+", "-", candidate)
    candidate = re.sub(r"[^a-z0-9-]", "", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-")

    return candidate or None
