"""Application configuration loading will live here."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    config_dir: Path = Path("config")
