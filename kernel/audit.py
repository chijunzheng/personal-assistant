"""Append-only JSONL audit log.

One file per day at ``<audit_root>/YYYY-MM-DD.jsonl``. Entries are validated
against the required-field set declared in ``configs/default.yaml`` before
they are persisted. The audit log is itself part of the kernel's
concurrency strategy (``kernel/SYNC.md`` defense #5): readers dedupe by
``id`` (sha256), so commutative replay is automatic.

This module deliberately exposes a small surface: a Pydantic model for
the entry plus a writer function. The writer never opens the file in
truncating mode — appends only.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AuditEntry", "REQUIRED_FIELDS", "write_audit_entry"]


# Mirrors ``audit.fields_required`` in ``configs/default.yaml``. Keep these
# two in sync — the config is the human-facing source of truth, this constant
# is what the kernel actually validates against.
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "ts",
    "op",
    "actor",
    "outcome",
    "duration_ms",
    "config",
)


class AuditEntry(BaseModel):
    """Schema for one audit-log line.

    All fields except ``id`` may be supplied by the caller; ``id`` is
    derived from a sha256 of the canonicalized payload at write time so
    that callers cannot accidentally produce non-idempotent entries.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    id: str = Field(default="")
    ts: str
    op: str
    actor: str
    outcome: str
    duration_ms: int
    config: str


def _entry_id(payload: Mapping[str, Any]) -> str:
    """Stable sha256 over the canonicalized payload (excluding any incoming id)."""
    canonical = {k: v for k, v in payload.items() if k != "id"}
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _date_for(entry: Mapping[str, Any]) -> date:
    """Pull the ``YYYY-MM-DD`` date used for daily rotation off the entry's ts."""
    ts_raw = entry["ts"]
    if isinstance(ts_raw, datetime):
        return ts_raw.date()
    # ``fromisoformat`` understands ISO8601 with offset on Python 3.11+.
    return datetime.fromisoformat(str(ts_raw)).date()


def write_audit_entry(
    entry: Mapping[str, Any],
    *,
    audit_root: str | os.PathLike[str],
) -> AuditEntry:
    """Validate and append one audit entry to the day's JSONL file.

    Returns the validated ``AuditEntry`` (with derived ``id``) so callers
    can correlate.

    Raises:
        ValueError: a required field is missing.
        pydantic.ValidationError: a field has the wrong type.
    """
    payload = dict(entry)

    missing = [f for f in REQUIRED_FIELDS if f != "id" and f not in payload]
    if missing:
        raise ValueError(
            f"audit entry missing required fields: {', '.join(sorted(missing))}"
        )

    payload["id"] = _entry_id(payload)
    validated = AuditEntry(**payload)

    root = Path(audit_root)
    root.mkdir(parents=True, exist_ok=True)
    day = _date_for(payload)
    target = root / f"{day.isoformat()}.jsonl"

    line = json.dumps(validated.model_dump(), separators=(",", ":"), default=str)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()

    return validated
