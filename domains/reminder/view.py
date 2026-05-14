"""Reminder plugin — projected markdown view.

One-way projection from ``vault/reminder/events.jsonl`` to a human-readable
``vault/reminder/reminders.md`` for Obsidian. The JSONL log is the source
of truth (append-only, sha256 ids — see ``kernel/SYNC.md``); the markdown
is regenerated on every write and never read back.

Issue #38 retrofitted this renderer onto the kernel view primitive
(ADR 2026-05-14). The signature is now the uniform kernel-view contract::

    def render(vault_root: Path, *, now: datetime) -> None: ...

The renderer is a pure function of canonical storage: it reads
``events.jsonl`` itself and writes ``reminders.md`` through
:func:`kernel.vault.atomic_write`. The kernel's ``_render_domain_view``
post-write hook drives it — the handler no longer self-calls it.

What makes the read-back-forbidden contract safe is machine-checkable:
the view carries a ``generated_by:`` frontmatter marker, and both
``kernel/index.py`` and ``kernel/retrieval.py`` key off that marker to keep
generated files out of their read paths.

Reminder is a live-state domain (ADR change 3): it projects to a single
file, not dated monthly rollups. The view also carries deterministic
structural ``[[wikilinks]]`` to journal notes from the current calendar
month — pure functions of dates and filesystem layout, no LLM — which
light up ``INDEX.md``'s existing backlink graph.

Slice 1 (issue #32): pending reminders only. Fired + cancelled history
sections arrive in issue #33.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from kernel.vault import atomic_write

__all__ = ["render"]


_REMINDER_DIR = "reminder"
_JOURNAL_DIR = "journal"
_EVENTS_RELATIVE = Path(_REMINDER_DIR) / "events.jsonl"
_VIEW_RELATIVE = Path(_REMINDER_DIR) / "reminders.md"

_HEADER_TITLE = "# Reminders"
_ID_PREFIX_LEN = 6

# The marker every generated file carries — index.py and retrieval.py both
# match on this exact key to keep generated views out of their read paths.
_GENERATED_BY = "domains/reminder/view.py"

# A journal note filename is ``YYYY-MM-DD-slug.md``; we link notes whose
# leading ``YYYY-MM`` matches the view's reference month.
_MONTH_PREFIX_LEN = len("YYYY-MM")


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


def _journal_notes_for_month(vault_root: Path, month: str) -> list[str]:
    """Return sorted journal-note stems whose date prefix is in ``month``.

    Stems (no ``.md``) so they slot straight into a ``[[journal/<stem>]]``
    wikilink. Missing ``journal/`` directory yields an empty list.
    """
    journal_dir = vault_root / _JOURNAL_DIR
    if not journal_dir.is_dir():
        return []
    stems = [
        path.stem
        for path in journal_dir.glob("*.md")
        if path.stem[:_MONTH_PREFIX_LEN] == month
    ]
    return sorted(stems)


def _build_document(
    *,
    rows: list[dict],
    now: datetime,
    journal_stems: list[str],
) -> str:
    """Compose the full ``reminders.md`` document.

    The ``generated_by:`` marker and the ``generated_at:`` timestamp live
    in frontmatter; ``generated_at`` is the only place ``now`` lands so the
    body stays a pure function of the JSONL.
    """
    timestamp = now.isoformat()
    lines: list[str] = [
        "---",
        f"generated_by: {_GENERATED_BY}",
        f"generated_at: {timestamp}",
        "---",
        "",
        _HEADER_TITLE,
        f"_Last updated: {timestamp} (generated — do not edit)_",
    ]

    pending_lines = [_format_pending(row) for row in _pending(rows)]
    if pending_lines:
        lines.append("")
        lines.append("## Pending")
        lines.extend(pending_lines)

    # Cross-domain structural links — journal notes from the same month.
    if journal_stems:
        lines.append("")
        lines.append("## Related journal notes")
        lines.extend(f"- [[{_JOURNAL_DIR}/{stem}]]" for stem in journal_stems)

    return "\n".join(lines) + "\n"


def render(vault_root: str | Path, *, now: datetime) -> None:
    """Regenerate ``reminders.md`` from the canonical ``events.jsonl``.

    Pure function of canonical storage: the same JSONL produces
    byte-identical markdown (modulo the ``generated_at:`` timestamp, the
    sole landing place for ``now``). Writes ``vault/reminder/reminders.md``
    through :func:`kernel.vault.atomic_write` so a syncing process never
    sees a half-written file and an unchanged log produces no churn.

    Args:
        vault_root: vault root on disk. A missing ``events.jsonl`` is
            treated as an empty log — a header-only view is still written.
        now: reference timestamp; lands only in the ``generated_at:``
            frontmatter field and the "Last updated" header line.
    """
    root = Path(vault_root)
    rows = list(_iter_events(root / _EVENTS_RELATIVE))
    month = now.isoformat()[:_MONTH_PREFIX_LEN]
    body = _build_document(
        rows=rows,
        now=now,
        journal_stems=_journal_notes_for_month(root, month),
    )
    atomic_write(root / _VIEW_RELATIVE, body)
