"""Tests for ``domains.journal.digest`` — weekly "topics you've been thinking about".

The digest's contract: given a vault root and a window, return a
markdown-friendly string enumerating journal entries with their tags
within the window. When nothing is captured, return an empty string so
the assembler can omit the section cleanly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from domains.journal.digest import summarize


def _seed_journal_entry(
    vault_root: Path,
    *,
    filename: str,
    date_iso: str,
    body: str,
    tags: list[str],
) -> Path:
    """Drop a journal markdown file directly so digest tests start populated."""
    journal = vault_root / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    tag_block = ", ".join(tags)
    content = (
        f"---\n"
        f"date: {date_iso}\n"
        f"tags: [{tag_block}]\n"
        f"links: []\n"
        f"source: telegram\n"
        f"session_id: test\n"
        f"---\n\n"
        f"{body}\n"
    )
    path = journal / filename
    path.write_text(content, encoding="utf-8")
    return path


def test_summarize_lists_entries_inside_window(tmp_path: Path) -> None:
    """Entries whose date falls inside the window appear in the summary."""
    vault_root = tmp_path / "vault"
    _seed_journal_entry(
        vault_root,
        filename="2026-04-29-memgpt.md",
        date_iso="2026-04-29T08:00:00+00:00",
        body="Reading the memgpt paper",
        tags=["agents", "memory"],
    )
    _seed_journal_entry(
        vault_root,
        filename="2026-04-25-rag.md",
        date_iso="2026-04-25T08:00:00+00:00",
        body="Thinking about RAG",
        tags=["rag"],
    )

    since = datetime(2026, 4, 22, tzinfo=timezone.utc)
    result = summarize(vault_root=vault_root, since=since)
    assert "memgpt" in result.lower()
    assert "rag" in result.lower()


def test_summarize_excludes_entries_before_window(tmp_path: Path) -> None:
    """Entries older than the window are not in the summary."""
    vault_root = tmp_path / "vault"
    _seed_journal_entry(
        vault_root,
        filename="2026-03-01-old.md",
        date_iso="2026-03-01T08:00:00+00:00",
        body="Old note",
        tags=["history"],
    )

    since = datetime(2026, 4, 22, tzinfo=timezone.utc)
    result = summarize(vault_root=vault_root, since=since)
    assert "old" not in result.lower() or result == ""


def test_summarize_returns_empty_when_no_journal_dir(tmp_path: Path) -> None:
    """Missing vault/journal/ directory degrades gracefully -> empty string."""
    vault_root = tmp_path / "vault"
    since = datetime(2026, 4, 22, tzinfo=timezone.utc)
    assert summarize(vault_root=vault_root, since=since) == ""


def test_summarize_default_since_is_seven_days(tmp_path: Path) -> None:
    """If no ``since`` arg is provided, default to last 7 days."""
    vault_root = tmp_path / "vault"
    # Just ensure no crash when called without ``since``; a freshly-created
    # journal dir but no entries should yield empty.
    (vault_root / "journal").mkdir(parents=True)
    assert summarize(vault_root=vault_root) == ""


def test_summarize_includes_tags_when_present(tmp_path: Path) -> None:
    """Entry tags surface in the digest line so themes are visible."""
    vault_root = tmp_path / "vault"
    _seed_journal_entry(
        vault_root,
        filename="2026-04-29-rag.md",
        date_iso="2026-04-29T08:00:00+00:00",
        body="RAG thoughts",
        tags=["rag", "evaluation"],
    )
    since = datetime(2026, 4, 22, tzinfo=timezone.utc)
    result = summarize(vault_root=vault_root, since=since)
    assert "rag" in result.lower()
    assert "evaluation" in result.lower()
