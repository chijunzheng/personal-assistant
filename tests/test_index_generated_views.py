"""Tests for ``kernel.index``'s marker-aware selective walk.

ADR 2026-05-14, change 5: ``index.py`` walks every ``.md`` under
``vault/<domain>/`` and now encounters the generated views. It identifies
them by the ``generated_by:`` frontmatter marker and treats them
selectively:

  - their ``[[wikilinks]]`` ARE harvested into the backlink graph (so the
    wiki works — a generated rollup linking a journal note rescues that
    note from the orphan list)
  - they are EXCLUDED from orphans, topic clusters, recent activity, and
    domain stats (so finance is not double-counted, and INDEX.md's
    eval-relevant sections do not shift)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from kernel.index import refresh


def _fixed_clock() -> datetime:
    return datetime(2026, 5, 14, 9, 0, 0, tzinfo=timezone.utc)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_GENERATED_ROLLUP = (
    "---\n"
    "generated_by: domains/finance/view.py\n"
    "generated_at: 2026-05-14T09:00:00+00:00\n"
    "---\n\n"
    "# Finance — 2026-03\n\n"
    "See also [[journal/2026-03-09-budget-thoughts-ab12]]\n"
)

_SOURCE_NOTE = (
    "---\n"
    "date: 2026-03-09\n"
    "tags: [budget]\n"
    "---\n\n"
    "# budget thoughts\n\n"
    "Some narrative.\n"
)


# ---------------------------------------------------------------------------
# Cycle 1 — generated views are excluded from domain stats / file counts
# ---------------------------------------------------------------------------


def test_generated_view_not_counted_in_files_indexed(tmp_path: Path) -> None:
    """A generated rollup must not inflate ``files_indexed``."""
    vault_root = tmp_path / "vault"
    _write(vault_root / "journal" / "2026-03-09-budget-thoughts-ab12.md", _SOURCE_NOTE)
    _write(vault_root / "finance" / "2026-03.md", _GENERATED_ROLLUP)

    result = refresh(vault_root, clock=_fixed_clock)

    # Only the journal note counts; the generated rollup does not.
    assert result.files_indexed == 1


def test_generated_view_absent_from_recent_activity(tmp_path: Path) -> None:
    """The generated rollup never appears in the Recent Activity section."""
    vault_root = tmp_path / "vault"
    _write(vault_root / "journal" / "2026-03-09-budget-thoughts-ab12.md", _SOURCE_NOTE)
    _write(vault_root / "finance" / "2026-03.md", _GENERATED_ROLLUP)

    refresh(vault_root, clock=_fixed_clock)
    index = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    recent = index.split("## Recent Activity", 1)[1].split("## ", 1)[0]
    assert "finance/2026-03.md" not in recent
    assert "2026-03-09-budget-thoughts-ab12.md" in recent


def test_generated_view_absent_from_topic_clusters(tmp_path: Path) -> None:
    """A generated view must not seed or join a topic cluster."""
    vault_root = tmp_path / "vault"
    # Generated view carries a tag — it must still be ignored for clustering.
    tagged_view = _GENERATED_ROLLUP.replace(
        "generated_at: 2026-05-14T09:00:00+00:00\n",
        "generated_at: 2026-05-14T09:00:00+00:00\ntags: [budget]\n",
    )
    _write(vault_root / "journal" / "2026-03-09-budget-thoughts-ab12.md", _SOURCE_NOTE)
    _write(vault_root / "finance" / "2026-03.md", tagged_view)

    refresh(vault_root, clock=_fixed_clock)
    index = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    clusters = index.split("## Topic Clusters", 1)[1].split("\n---\n", 1)[0]
    assert "finance/2026-03.md" not in clusters


# ---------------------------------------------------------------------------
# Cycle 2 — generated views' wikilinks ARE harvested into the backlink graph
# ---------------------------------------------------------------------------


def test_generated_view_wikilinks_rescue_journal_note_from_orphans(
    tmp_path: Path,
) -> None:
    """A journal note linked only by a generated rollup is not an orphan."""
    vault_root = tmp_path / "vault"
    # The journal note has no inbound/outbound links of its own...
    _write(vault_root / "journal" / "2026-03-09-budget-thoughts-ab12.md", _SOURCE_NOTE)
    # ...but the generated rollup links to it.
    _write(vault_root / "finance" / "2026-03.md", _GENERATED_ROLLUP)

    refresh(vault_root, clock=_fixed_clock)
    index = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    orphans = index.split("## Orphans", 1)[1].split("## ", 1)[0]
    # The generated link rescues the journal note from the orphan list.
    assert "2026-03-09-budget-thoughts-ab12.md" not in orphans


def test_generated_view_itself_never_listed_as_orphan(tmp_path: Path) -> None:
    """A generated view with no inbound links is still not flagged an orphan."""
    vault_root = tmp_path / "vault"
    # A generated view with zero outbound links and nothing linking to it.
    lonely_view = (
        "---\ngenerated_by: domains/finance/view.py\n---\n\n# Finance — 2026-03\n"
    )
    _write(vault_root / "finance" / "2026-03.md", lonely_view)

    refresh(vault_root, clock=_fixed_clock)
    index = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    orphans = index.split("## Orphans", 1)[1].split("## ", 1)[0]
    assert "finance/2026-03.md" not in orphans
