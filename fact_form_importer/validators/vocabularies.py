"""Controlled vocabulary loading and matching.

This module intentionally uses local JSON only. Later validators can replace or
augment the loaded data with FaCT API responses without changing the matching
call sites.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


_DEFAULT_VOCABULARIES: Optional["Vocabularies"] = None


def _normalise_for_match(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    text = re.sub(r"\s+", " ", text)
    text = text.casefold()
    text = text.replace("’", "'")
    return text


class VocabularyEntry(BaseModel):
    code: str
    name: str
    aliases: List[str] = Field(default_factory=list)

    @field_validator("code", "name")
    @classmethod
    def required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Vocabulary entry code and name must not be blank")
        return value

    @field_validator("aliases")
    @classmethod
    def aliases_must_not_be_blank(cls, values: List[str]) -> List[str]:
        for value in values:
            if not value.strip():
                raise ValueError("Vocabulary aliases must not be blank")
        return values

    def values(self) -> Iterable[str]:
        yield self.code
        yield self.name
        yield from self.aliases


class Vocabularies(BaseModel):
    version: Optional[str] = None
    vocabularies: Dict[str, List[VocabularyEntry]]

    @model_validator(mode="before")
    @classmethod
    def accept_top_level_vocabularies(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        if "vocabularies" in data:
            return data

        vocabularies = {
            key: value
            for key, value in data.items()
            if key != "version" and isinstance(value, list)
        }

        return {
            "version": data.get("version"),
            "vocabularies": vocabularies,
        }

    @field_validator("vocabularies")
    @classmethod
    def vocabularies_must_not_be_empty(
        cls, vocabularies: Dict[str, List[VocabularyEntry]]
    ) -> Dict[str, List[VocabularyEntry]]:
        if not vocabularies:
            raise ValueError("At least one vocabulary must be configured")

        for name, entries in vocabularies.items():
            if not name.strip():
                raise ValueError("Vocabulary names must not be blank")
            if not entries:
                raise ValueError(f"Vocabulary '{name}' must contain at least one entry")

        return vocabularies

    def get(self, vocabulary_name: str) -> List[VocabularyEntry]:
        return self.vocabularies.get(vocabulary_name, [])

    def exact_vocab_match(
        self, raw_value: Any, vocabulary_name: str
    ) -> Optional[VocabularyEntry]:
        if raw_value is None:
            return None

        value = str(raw_value).strip()
        if not value:
            return None

        for entry in self.get(vocabulary_name):
            if any(value == candidate for candidate in entry.values()):
                return entry

        return None

    def normalised_vocab_match(
        self, raw_value: Any, vocabulary_name: str
    ) -> Optional[VocabularyEntry]:
        normalised_value = _normalise_for_match(raw_value)
        if normalised_value is None:
            return None

        for entry in self.get(vocabulary_name):
            if any(normalised_value == _normalise_for_match(candidate) for candidate in entry.values()):
                return entry

        return None

    def value_in_vocab(self, code_or_name: Any, vocabulary_name: str) -> bool:
        return self.normalised_vocab_match(code_or_name, vocabulary_name) is not None


def load_vocabularies(path: Path | str) -> Vocabularies:
    global _DEFAULT_VOCABULARIES

    vocabularies = Vocabularies(**json.loads(Path(path).read_text(encoding="utf-8")))
    _DEFAULT_VOCABULARIES = vocabularies
    return vocabularies


def _default_vocabularies() -> Vocabularies:
    global _DEFAULT_VOCABULARIES

    if _DEFAULT_VOCABULARIES is None:
        _DEFAULT_VOCABULARIES = load_vocabularies(Path("config/vocabularies.example.json"))

    return _DEFAULT_VOCABULARIES


def exact_vocab_match(raw_value: Any, vocabulary_name: str) -> Optional[VocabularyEntry]:
    return _default_vocabularies().exact_vocab_match(raw_value, vocabulary_name)


def normalised_vocab_match(raw_value: Any, vocabulary_name: str) -> Optional[VocabularyEntry]:
    return _default_vocabularies().normalised_vocab_match(raw_value, vocabulary_name)


def value_in_vocab(code_or_name: Any, vocabulary_name: str) -> bool:
    return _default_vocabularies().value_in_vocab(code_or_name, vocabulary_name)
