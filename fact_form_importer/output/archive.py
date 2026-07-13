"""Publish immutable completed-run archives and maintain a latest-run mirror."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ARCHIVE_DIRECTORY = "final"
STAGING_DIRECTORY = ".staging"
LATEST_RUN_FILE = "latest_run.json"
RUN_MANIFEST_FILE = "run_manifest.json"


@dataclass(frozen=True)
class ArchiveResult:
    run_id: str
    archive_path: Path
    latest_pointer_path: Path
    manifest: dict[str, Any]


def stage_path(output_root: Path, run_id: str) -> Path:
    return output_root / STAGING_DIRECTORY / run_id


def publish_run_archive(
    output_root: Path,
    staging_path: Path,
    run_id: str,
    source_name: str,
    summary: dict[str, Any],
) -> ArchiveResult:
    """Atomically publish a completed staging directory as an immutable run archive."""

    archive_root = output_root / ARCHIVE_DIRECTORY
    archive_path = archive_root / run_id
    if archive_path.exists():
        raise ValueError(f"Run archive already exists: {archive_path}")
    if not staging_path.exists():
        raise ValueError(f"Run staging directory does not exist: {staging_path}")

    manifest = _build_manifest(run_id, source_name, staging_path, summary)
    (staging_path / RUN_MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    archive_root.mkdir(parents=True, exist_ok=True)
    staging_path.replace(archive_path)

    _mirror_latest_artifacts(output_root, archive_path)
    latest_payload = {
        "run_id": run_id,
        "archive_path": str(archive_path),
        "completed_at": manifest["completed_at"],
    }
    latest_pointer_path = output_root / LATEST_RUN_FILE
    latest_pointer_path.write_text(
        json.dumps(latest_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return ArchiveResult(run_id, archive_path, latest_pointer_path, manifest)


def list_run_archives(output_root: Path) -> list[dict[str, Any]]:
    """Return valid archives newest first, ignoring incomplete staging directories."""

    archive_root = output_root / ARCHIVE_DIRECTORY
    if not archive_root.exists():
        return []

    archives = []
    for path in archive_root.iterdir():
        manifest_path = path / RUN_MANIFEST_FILE
        if not path.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if manifest.get("run_id") != path.name:
            continue
        archives.append({"path": path, "manifest": manifest})
    return sorted(archives, key=lambda archive: archive["manifest"].get("completed_at", ""), reverse=True)


def load_run_archive(output_root: Path, run_id: str) -> dict[str, Any] | None:
    for archive in list_run_archives(output_root):
        if archive["manifest"]["run_id"] == run_id:
            return archive
    return None


def _build_manifest(
    run_id: str, source_name: str, staging_path: Path, summary: dict[str, Any]
) -> dict[str, Any]:
    artifacts = []
    for path in sorted(staging_path.iterdir()):
        if path.is_file() and path.name != RUN_MANIFEST_FILE:
            artifacts.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    return {
        "run_id": run_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source_name": source_name,
        "summary": summary,
        "artifacts": artifacts,
    }


def _mirror_latest_artifacts(output_root: Path, archive_path: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    for path in archive_path.iterdir():
        if path.is_file():
            shutil.copy2(path, output_root / path.name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
