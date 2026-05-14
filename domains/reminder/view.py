"""Reminder plugin — projected markdown view.

One-way projection from ``vault/reminder/events.jsonl`` to a human-readable
``vault/reminder/reminders.md`` for Obsidian. The JSONL log is the source
of truth (append-only, sha256 ids — see ``kernel/SYNC.md``); the markdown
is regenerated on every write and never read back.

Slice 1 (issue #32): pending reminders only. Fired + cancelled history
sections arrive in issue #33.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

__all__ = ["render"]


_HEADER_TITLE = "# Reminders"
_ID_PREFIX_LEN = 6


def _iter_events(path: Path) -> Iterable[dict]:
    """Yield each event row from the JSONL log; skip blank/bad lines."""
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


def _short_id(row: Mapping[str, object]) -> str:
    raw = row.get("id")
    return str(raw)[:_ID_PREFIX_LEN] if isinstance(raw, str) else "?"


def _retired_ids(rows: list[dict]) -> set[str]:
    """Collect ids of reminders that are no longer pending.

    A reminder is retired when:
      - a later ``kind == 'cancel'`` event names it via ``target_id``,
      - the row's own ``status`` was advanced to ``fired`` or ``cancelled``.
    """
    retired: set[str] = set()
    for row in rows:
        if row.get("kind") == "cancel":
            target = row.get("target_id")
            if isinstance(target, str):
                retired.add(target)
            continue
        status = row.get("status")
        if status in ("fired", "cancelled"):
            target = row.get("target_id") or row.get("id")
            if isinstance(target, str):
                retired.add(target)
    return retired


def _pending(rows: list[dict]) -> list[dict]:
    """Project the event log to the still-pending reminder rows, in event order."""
    retired = _retired_ids(rows)
    out: list[dict] = []
    for row in rows:
        if row.get("kind") == "cancel":
            continue  # bookkeeping only
        if row.get("status") != "pending":
            continue
        rid = row.get("id")
        if isinstance(rid, str) and rid in retired:
            continue
        out.append(row)
    return out


def _format_pending(row: Mapping[str, object]) -> str:
    """One pending reminder row as an Obsidian Tasks checkbox line."""
    message = str(row.get("message") or "").strip()
    kind = str(row.get("kind") or "").strip()
    short = _short_id(row)
    if kind == "scheduled":
        fire_at = str(row.get("fire_at") or "").strip()
        suffix = f" — {fire_at}" if fire_at else ""
        return f"- [ ] {message}{suffix} <!-- id: {short} | scheduled -->"
    if kind == "state_derived":
        condition = str(row.get("condition") or "").strip()
        suffix = f" — when {condition}" if condition else ""
        return f"- [ ] {message}{suffix} <!-- id: {short} | state_derived -->"
    return f"- [ ] {message} <!-- id: {short} | {kind} -->"


def render(events_path: Path, *, now: datetime) -> str:
    """Project the events log into the Obsidian-readable markdown view.

    Pure function: same inputs produce byte-identical output. The ``now``
    kwarg is explicit so callers control reproducibility (the resulting
    timestamp lands in the header).

    Args:
        events_path: path to the canonical ``events.jsonl``. Missing file
            is treated as an empty log.
        now: reference timestamp for the "Last updated" header line.

    Returns:
        The full markdown document as a string, ready to be written
        atomically. Always ends with a trailing newline.
    """
    timestamp = now.isoformat()
    sections = [
        _HEADER_TITLE,
        f"_Last updated: {timestamp} (generated — do not edit)_",
    ]

    rows = list(_iter_events(events_path))
    pending_lines = [_format_pending(row) for row in _pending(rows)]
    if pending_lines:
        sections.append("")
        sections.append("## Pending")
        sections.extend(pending_lines)

    return "\n".join(sections) + "\n"
