"""Cross-process locking and crash-safe JSON persistence for mutable sidecars."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterator


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[object]:
    """Hold an advisory exclusive lock shared by every importer process."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def atomic_write_json(path: Path, payload: Any, *, backup: bool = True) -> None:
    """Write JSON through a unique same-directory file and atomic replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        else:
            backup_path = path.with_suffix(path.suffix + ".bak")
            _atomic_copy(path, backup_path)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_copy(source: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def decode_first_json_object(text: str) -> tuple[Any, str]:
    """Decode a valid leading JSON value and return any non-whitespace suffix."""

    value, end = json.JSONDecoder().raw_decode(text)
    return value, text[end:].strip()
