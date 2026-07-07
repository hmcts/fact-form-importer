"""Application configuration loading."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

@dataclass(frozen=True)
class AppConfig:
    config_dir: Path = Path("config")

    @property
    def field_rules_path(self) -> Path:
        return self.config_dir / "field_rules.json"

    @property
    def vocabularies_path(self) -> Path:
        return self.config_dir / "vocabularies.example.json"


class LlmRule(BaseModel):
    enabled: bool = False
    purpose: Optional[str] = None
    use_only_when: Optional[str] = None
    rules: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_purpose_and_rules_when_enabled(self) -> "LlmRule":
        if self.enabled:
            if not self.purpose:
                raise ValueError("LLM-enabled fields must declare a purpose")
            if not self.rules:
                raise ValueError("LLM-enabled fields must declare at least one rule")
        return self


class FieldRule(BaseModel):
    required: bool = False
    cleaners: list[str] = Field(default_factory=list)
    validators: list[str] = Field(default_factory=list)
    llm: LlmRule = Field(default_factory=LlmRule)
    notes: Optional[str] = None

    @field_validator("cleaners", "validators")
    @classmethod
    def names_must_not_be_blank(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or not value.strip():
                raise ValueError("Cleaner and validator names must not be blank")
        return values


class FieldRulesConfig(BaseModel):
    version: str
    fields: dict[str, FieldRule]

    @field_validator("version")
    @classmethod
    def version_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Field rules version must not be blank")
        return value

    @field_validator("fields")
    @classmethod
    def fields_must_not_be_empty(cls, fields: dict[str, FieldRule]) -> dict[str, FieldRule]:
        if not fields:
            raise ValueError("Field rules must contain at least one field")

        for field_name in fields:
            if not field_name.strip():
                raise ValueError("Field rule names must not be blank")

        return fields


def load_field_rules(path: Path) -> FieldRulesConfig:
    return FieldRulesConfig(**json.loads(path.read_text(encoding="utf-8")))


def load_default_field_rules(config: Optional[AppConfig] = None) -> FieldRulesConfig:
    app_config = config or AppConfig()
    return load_field_rules(app_config.field_rules_path)
