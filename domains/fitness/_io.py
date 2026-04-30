"""Shared I/O helpers for the fitness plugin.

These primitives are concurrency-safe (every write goes through
``kernel.vault.atomic_write`` so a Drive sync mid-write never observes a
half-written file) and tolerant of missing/malformed files (callers can
treat first-run vaults the same as steady-state vaults).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import yaml

from kernel.vault import atomic_write

from domains.fitness._paths import (
    ALIASES_RELATIVE,
    PROFILE_RELATIVE,
    TEMPLATE_PATH,
)

__all__ = [
    "append_jsonl",
    "ensure_profile_bootstrapped",
    "existing_ids",
    "iter_jsonl",
    "load_aliases",
    "load_yaml",
    "now",
    "sha256_parts",
    "store_binary_atomically",
]


def now(clock: Optional[Callable[[], datetime]] = None) -> datetime:
    """Return ``datetime.now(tz=utc)`` unless a pluggable clock overrides."""
    return (clock or (lambda: datetime.now(tz=timezone.utc)))()


def sha256_parts(parts: Iterable[str]) -> str:
    """Stable sha256 over ``|``-joined parts; convenience for content ids."""
    digest = hashlib.sha256()
    digest.update("|".join(parts).encode("utf-8"))
    return digest.hexdigest()


def iter_jsonl(path: Path) -> Iterable[dict]:
    """Yield each row from a JSONL log; tolerate missing/malformed lines."""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def existing_ids(path: Path) -> set[str]:
    """Return every id field from a JSONL file; tolerate a missing file."""
    seen: set[str] = set()
    for row in iter_jsonl(path):
        row_id = row.get("id")
        if isinstance(row_id, str):
            seen.add(row_id)
    return seen


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    """Append one row to the JSONL file via ``atomic_write``."""
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing = existing + "\n"
    line = json.dumps(dict(row), default=str) + "\n"
    atomic_write(path, existing + line)


def load_yaml(path: Path) -> dict:
    """Read a YAML file; return ``{}`` when missing or malformed."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def ensure_profile_bootstrapped(vault_root: Path) -> None:
    """Copy ``profile.template.yaml`` to ``vault/fitness/profile.yaml`` first-run.

    Idempotent: when the vault file already exists, this is a no-op (the
    user's filled-in profile is preserved).
    """
    target = vault_root / PROFILE_RELATIVE
    if target.exists():
        return
    try:
        contents = TEMPLATE_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        # Template missing — fall back to an empty stub so writes don't
        # crash. The user can hand-fill profile.yaml later.
        contents = ""
    atomic_write(target, contents)


def load_aliases(vault_root: Path) -> dict[str, str]:
    """Load ``_exercise_aliases.yaml`` if present; return ``{}`` otherwise.

    The map is lowercase-keyed for case-insensitive lookup. Missing file
    is non-fatal — extraction falls back to whatever the LLM produced.
    """
    path = vault_root / ALIASES_RELATIVE
    raw = load_yaml(path)
    return {str(k).lower().strip(): str(v) for k, v in raw.items() if isinstance(k, str)}


def store_binary_atomically(*, path: Path, payload: bytes) -> None:
    """Persist binary bytes via tmp+os.replace (atomic_write is text-only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
