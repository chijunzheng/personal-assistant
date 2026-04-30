"""Journal weekly digest contributor — "topics you've been thinking about".

The digest assembler (``kernel.proactive`` weekly task) calls
``summarize(vault_root=..., since=...)`` once per scheduled run. The
function reads markdown files under ``vault/journal/`` whose frontmatter
``date`` falls inside the window and returns a markdown-friendly
enumeration with each entry's tags so emerging themes are visible.

When nothing falls inside the window, returns an empty string so the
assembler can omit the section cleanly. The proactive layer wraps this
output with an LLM advisory pass (issue #9 GREEN-band:
``suggested_actions=true``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

__all__ = ["summarize"]


_DEFAULT_WINDOW_DAYS = 7


@dataclass(frozen=True)
class _Entry:
    """Internal projection of one journal markdown file."""

    path: Path
    date: datetime
    tags: tuple[str, ...]
    title: str


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split ``raw`` into (frontmatter dict, body). Tolerate missing fences."""
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, raw
    if not isinstance(meta, dict):
        return {}, raw
    body = parts[2].lstrip("\n").rstrip()
    return meta, body


def _coerce_date(raw: object) -> Optional[datetime]:
    """Parse the frontmatter ``date`` to a tz-aware datetime, or None."""
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coerce_tags(raw: object) -> tuple[str, ...]:
    """Normalize the frontmatter tags list to a tuple of strings."""
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(t).strip() for t in raw if str(t).strip())


def _title_from_body(body: str, fallback: str) -> str:
    """First non-empty line of the body, or ``fallback`` if absent."""
    for line in body.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return fallback


def _load_entries(journal_dir: Path) -> list[_Entry]:
    """Return one ``_Entry`` per parseable journal markdown file."""
    entries: list[_Entry] = []
    if not journal_dir.exists():
        return entries
    for path in sorted(journal_dir.glob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = _parse_frontmatter(raw)
        date = _coerce_date(meta.get("date"))
        if date is None:
            continue
        tags = _coerce_tags(meta.get("tags"))
        title = _title_from_body(body, fallback=path.stem)
        entries.append(_Entry(path=path, date=date, tags=tags, title=title))
    return entries


def _format_line(entry: _Entry) -> str:
    """One bullet line: date, title, tag block (if any)."""
    date_str = entry.date.date().isoformat()
    tag_block = ""
    if entry.tags:
        tag_block = " — tags: " + ", ".join(entry.tags)
    return f"- {date_str}: {entry.title}{tag_block}"


def summarize(
    *,
    vault_root: str | os.PathLike[str],
    since: Optional[datetime] = None,
) -> str:
    """Return a markdown enumeration of journal entries since ``since``.

    Args:
        vault_root: vault root on disk; missing ``journal/`` is non-fatal.
        since: lower-bound (inclusive) for an entry's frontmatter ``date``.
            Defaults to seven days before now (UTC).

    Returns:
        A section like::

            ## Journal: topics this week

            - 2026-04-29: Reading the memgpt paper — tags: agents, memory
            - 2026-04-25: RAG thoughts — tags: rag

        Or an empty string when no entries fall inside the window.
    """
    journal_dir = Path(vault_root) / "journal"
    if since is None:
        since = datetime.now(tz=timezone.utc) - timedelta(days=_DEFAULT_WINDOW_DAYS)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    entries = [e for e in _load_entries(journal_dir) if e.date >= since]
    if not entries:
        return ""

    # Most-recent first reads naturally for a weekly digest.
    entries.sort(key=lambda e: e.date, reverse=True)
    lines = [_format_line(e) for e in entries]
    return "## Journal: topics this week\n\n" + "\n".join(lines) + "\n"
