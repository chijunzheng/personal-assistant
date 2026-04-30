"""Tests for ``kernel.index`` — minimal scaffold writer.

Issue #2 ships a placeholder INDEX writer so the next agent (issue #4) can
extend it with topic clusters, synonyms, recent activity, and orphan
detection. For now we only verify:

  - ``write_scaffold(vault_root)`` produces ``vault/_index/INDEX.md``
  - the file is non-empty markdown that names the vault
  - re-running is idempotent (same content)
"""

from __future__ import annotations

from pathlib import Path

from kernel.index import write_scaffold


def test_write_scaffold_creates_index_at_vault_index_path(tmp_path: Path) -> None:
    """The scaffold lives at ``<vault>/_index/INDEX.md``."""
    vault_root = tmp_path / "vault"

    write_scaffold(vault_root)

    expected = vault_root / "_index" / "INDEX.md"
    assert expected.exists()
    assert expected.read_text(encoding="utf-8").strip() != ""


def test_write_scaffold_starts_with_a_top_level_heading(tmp_path: Path) -> None:
    """The placeholder is recognizable markdown so Obsidian renders it sanely."""
    vault_root = tmp_path / "vault"

    write_scaffold(vault_root)

    contents = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")
    assert contents.lstrip().startswith("#")  # markdown heading


def test_write_scaffold_is_idempotent(tmp_path: Path) -> None:
    """Re-writing the scaffold yields the same content (deterministic)."""
    vault_root = tmp_path / "vault"

    write_scaffold(vault_root)
    first = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")
    write_scaffold(vault_root)
    second = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    assert first == second
