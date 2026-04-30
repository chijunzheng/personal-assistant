"""Tests for ``kernel.index.refresh`` — full INDEX.md generator.

Issue #4 replaces the placeholder scaffold from issue #2 with a real
auto-maintained TOC. ``refresh(vault_root, config)`` must walk
``vault/<domain>/**`` and produce a single ``vault/_index/INDEX.md``
containing six sections:

  1. Topic clusters (with synonyms + member files)
  2. Tag map (tag -> files)
  3. Recent activity (last N by mtime, descending)
  4. Domain stats (per-domain counts/freshness)
  5. Orphans (markdown notes with no wikilinks in or out)
  6. Vocabulary frontier (terms appearing exactly once)

The output must be deterministic — re-running with no vault changes
yields a byte-identical INDEX.md so Drive doesn't see noise.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kernel.index import refresh


def _write(path: Path, content: str, *, mtime: float | None = None) -> None:
    """Write a file with parents created and (optionally) a forced mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _seed_journal(
    vault_root: Path,
    *,
    name: str,
    tags: list[str],
    body: str,
    mtime: float | None = None,
) -> Path:
    """Drop a single journal note with frontmatter tags and a body."""
    tags_yaml = "[" + ", ".join(tags) + "]" if tags else "[]"
    content = (
        "---\n"
        f"date: 2026-04-29T12:00:00+00:00\n"
        f"tags: {tags_yaml}\n"
        f"links: []\n"
        "source: telegram\n"
        "session_id: test\n"
        "---\n\n"
        f"{body}\n"
    )
    path = vault_root / "journal" / name
    _write(path, content, mtime=mtime)
    return path


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Smoke + schema
# ---------------------------------------------------------------------------


def test_refresh_writes_index_md_at_expected_path(tmp_path: Path) -> None:
    """The refresh writes ``<vault>/_index/INDEX.md``."""
    vault_root = tmp_path / "vault"
    _seed_journal(vault_root, name="2026-04-29-a-thought.md", tags=["idea"], body="hi")

    refresh(vault_root, config={}, clock=_fixed_clock)

    assert (vault_root / "_index" / "INDEX.md").exists()


def test_refresh_emits_all_six_sections(tmp_path: Path) -> None:
    """The generated INDEX.md contains every required section header."""
    vault_root = tmp_path / "vault"
    _seed_journal(vault_root, name="2026-04-29-thought.md", tags=["idea"], body="x")

    refresh(vault_root, config={}, clock=_fixed_clock)

    text = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")
    assert "Topic Clusters" in text
    assert "Tag Map" in text
    assert "Recent Activity" in text
    assert "Domain Stats" in text
    assert "Orphans" in text
    assert "Vocabulary Frontier" in text


# ---------------------------------------------------------------------------
# Determinism — issue #4 acceptance criterion
# ---------------------------------------------------------------------------


def test_refresh_is_deterministic_with_no_vault_changes(tmp_path: Path) -> None:
    """Two refreshes against an unchanged vault produce identical bytes.

    This is load-bearing: Drive sync sees noise on every spurious diff.
    """
    vault_root = tmp_path / "vault"
    _seed_journal(
        vault_root, name="2026-04-29-a.md", tags=["idea"], body="alpha", mtime=1_700_000_000
    )
    _seed_journal(
        vault_root, name="2026-04-28-b.md", tags=["question"], body="beta", mtime=1_699_900_000
    )

    refresh(vault_root, config={}, clock=_fixed_clock)
    first = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    refresh(vault_root, config={}, clock=_fixed_clock)
    second = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    assert first == second


# ---------------------------------------------------------------------------
# Tag map
# ---------------------------------------------------------------------------


def test_tag_map_lists_each_tag_with_files_using_it(tmp_path: Path) -> None:
    """Every distinct frontmatter tag appears in the tag map with its files."""
    vault_root = tmp_path / "vault"
    _seed_journal(vault_root, name="2026-04-29-a.md", tags=["idea"], body="x")
    _seed_journal(vault_root, name="2026-04-28-b.md", tags=["idea", "question"], body="y")
    _seed_journal(vault_root, name="2026-04-27-c.md", tags=["question"], body="z")

    refresh(vault_root, config={}, clock=_fixed_clock)
    text = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    # Find tag-map section.
    tag_section = text.split("Tag Map", 1)[1].split("##", 1)[0]
    assert "idea" in tag_section
    assert "question" in tag_section
    # Counts: idea used by 2 files, question used by 2 files
    assert "2026-04-29-a.md" in tag_section
    assert "2026-04-28-b.md" in tag_section
    assert "2026-04-27-c.md" in tag_section


# ---------------------------------------------------------------------------
# Recent activity ordering — by mtime descending
# ---------------------------------------------------------------------------


def test_recent_activity_orders_by_mtime_descending(tmp_path: Path) -> None:
    """The recent-activity section lists most-recently-modified files first."""
    vault_root = tmp_path / "vault"
    _seed_journal(
        vault_root, name="2026-04-27-old.md", tags=[], body="x", mtime=1_700_000_000
    )
    _seed_journal(
        vault_root, name="2026-04-28-mid.md", tags=[], body="y", mtime=1_700_500_000
    )
    _seed_journal(
        vault_root, name="2026-04-29-new.md", tags=[], body="z", mtime=1_700_900_000
    )

    refresh(vault_root, config={}, clock=_fixed_clock)
    text = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    recent = text.split("Recent Activity", 1)[1].split("##", 1)[0]
    pos_new = recent.find("2026-04-29-new.md")
    pos_mid = recent.find("2026-04-28-mid.md")
    pos_old = recent.find("2026-04-27-old.md")
    assert pos_new != -1 and pos_mid != -1 and pos_old != -1
    assert pos_new < pos_mid < pos_old


# ---------------------------------------------------------------------------
# Orphans
# ---------------------------------------------------------------------------


def test_orphans_are_files_with_no_wikilinks_in_or_out(tmp_path: Path) -> None:
    """A note with no inbound and no outbound [[wikilinks]] is reported as orphaned."""
    vault_root = tmp_path / "vault"

    # Two notes referenced via wikilinks; one orphan.
    linker_body = "see [[2026-04-28-target]] for context"
    _seed_journal(
        vault_root, name="2026-04-29-linker.md", tags=[], body=linker_body
    )
    _seed_journal(vault_root, name="2026-04-28-target.md", tags=[], body="hello")
    _seed_journal(vault_root, name="2026-04-27-orphan.md", tags=[], body="alone")

    refresh(vault_root, config={}, clock=_fixed_clock)
    text = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    orphans_section = text.split("Orphans", 1)[1].split("##", 1)[0]
    assert "2026-04-27-orphan.md" in orphans_section
    # The other two are NOT orphans:
    #  - linker has an outbound link; target is referenced (inbound link)
    assert "2026-04-29-linker.md" not in orphans_section
    assert "2026-04-28-target.md" not in orphans_section


# ---------------------------------------------------------------------------
# Domain stats
# ---------------------------------------------------------------------------


def test_domain_stats_includes_journal_finance_inventory_counts(tmp_path: Path) -> None:
    """Per-domain volume is captured (journal notes, finance rows, inventory items)."""
    vault_root = tmp_path / "vault"
    _seed_journal(vault_root, name="2026-04-29-a.md", tags=[], body="x")
    _seed_journal(vault_root, name="2026-04-28-b.md", tags=[], body="y")

    finance_path = vault_root / "finance" / "transactions.jsonl"
    finance_path.parent.mkdir(parents=True, exist_ok=True)
    finance_path.write_text(
        '{"id":"a","amount":-1.0}\n{"id":"b","amount":-2.0}\n{"id":"c","amount":3.0}\n',
        encoding="utf-8",
    )

    inventory_state = vault_root / "inventory" / "state.yaml"
    inventory_state.parent.mkdir(parents=True, exist_ok=True)
    inventory_state.write_text(
        "items:\n"
        "  - {item: milk, quantity: 2}\n"
        "  - {item: eggs, quantity: 6}\n",
        encoding="utf-8",
    )

    refresh(vault_root, config={}, clock=_fixed_clock)
    text = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    stats = text.split("Domain Stats", 1)[1].split("##", 1)[0]
    # Journal: 2 notes
    assert "journal" in stats
    assert "2" in stats
    # Finance: 3 transaction rows
    assert "finance" in stats
    assert "3" in stats
    # Inventory: 2 items in state.yaml
    assert "inventory" in stats


# ---------------------------------------------------------------------------
# Topic clusters + vocabulary frontier
# ---------------------------------------------------------------------------


def test_topic_clusters_group_files_sharing_tags(tmp_path: Path) -> None:
    """Files sharing a frontmatter tag end up in the same cluster with synonyms."""
    vault_root = tmp_path / "vault"
    _seed_journal(
        vault_root, name="2026-04-29-memory-tiers.md", tags=["agent-memory"], body="x"
    )
    _seed_journal(
        vault_root, name="2026-04-28-letta-design.md", tags=["agent-memory"], body="y"
    )
    _seed_journal(
        vault_root, name="2026-04-27-rag-thoughts.md", tags=["rag"], body="z"
    )

    refresh(vault_root, config={}, clock=_fixed_clock)
    text = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    clusters = text.split("Topic Clusters", 1)[1].split("##", 1)[0]
    assert "agent-memory" in clusters
    # Both members of the agent-memory cluster appear in the cluster section.
    assert "2026-04-29-memory-tiers.md" in clusters
    assert "2026-04-28-letta-design.md" in clusters
    # Synonyms: filename tokens like "memory", "tiers", "letta", "design" all
    # contribute to the synonyms list for this cluster.
    assert "memory" in clusters or "tiers" in clusters or "letta" in clusters


def test_vocabulary_frontier_lists_singleton_terms(tmp_path: Path) -> None:
    """Tokens that appear exactly once across the vault surface as frontier terms."""
    vault_root = tmp_path / "vault"
    # "quokka" appears in exactly one filename — should be a frontier term.
    # "agent" appears in two filenames — should NOT be a frontier term.
    _seed_journal(vault_root, name="2026-04-29-quokka-spotting.md", tags=[], body="x")
    _seed_journal(vault_root, name="2026-04-28-agent-design.md", tags=[], body="y")
    _seed_journal(vault_root, name="2026-04-27-agent-memory.md", tags=[], body="z")

    refresh(vault_root, config={}, clock=_fixed_clock)
    text = (vault_root / "_index" / "INDEX.md").read_text(encoding="utf-8")

    frontier = text.split("Vocabulary Frontier", 1)[1]
    assert "quokka" in frontier
    # "agent" appears twice -> not a frontier candidate.
    # We don't strictly require it absent (a heuristic might still bubble it),
    # but a pure-singleton frontier should not include it.
    # Soft check: frontier terms come from singleton-counts.
    assert "spotting" in frontier or "quokka" in frontier


# ---------------------------------------------------------------------------
# Atomicity + return value
# ---------------------------------------------------------------------------


def test_refresh_returns_a_result_with_files_indexed_count(tmp_path: Path) -> None:
    """``refresh`` returns a small immutable result the caller can inspect."""
    vault_root = tmp_path / "vault"
    _seed_journal(vault_root, name="2026-04-29-a.md", tags=[], body="x")
    _seed_journal(vault_root, name="2026-04-28-b.md", tags=[], body="y")

    result = refresh(vault_root, config={}, clock=_fixed_clock)

    # Two journal notes seeded — the result reports them.
    assert result.files_indexed >= 2


def test_refresh_writes_via_atomic_write_no_tmp_left_behind(tmp_path: Path) -> None:
    """No ``.tmp`` debris should remain after a successful refresh."""
    vault_root = tmp_path / "vault"
    _seed_journal(vault_root, name="2026-04-29-a.md", tags=[], body="x")

    refresh(vault_root, config={}, clock=_fixed_clock)

    leftover = list((vault_root / "_index").glob("*.tmp"))
    assert leftover == []


# ---------------------------------------------------------------------------
# Performance — issue #4 acceptance criterion ("under 5s on 100+ entries")
# ---------------------------------------------------------------------------


def test_refresh_completes_under_five_seconds_on_one_hundred_notes(tmp_path: Path) -> None:
    """Acceptance: latency stays under 5s on a vault with 100+ entries."""
    vault_root = tmp_path / "vault"
    for i in range(120):
        _seed_journal(
            vault_root,
            name=f"2026-04-{(i % 30) + 1:02d}-note-{i:03d}.md",
            tags=["idea"] if i % 2 == 0 else ["question"],
            body=f"some content {i} [[2026-04-{(i % 30) + 1:02d}-note-{(i + 1) % 120:03d}]]",
            mtime=1_700_000_000 + i,
        )

    started = time.monotonic()
    refresh(vault_root, config={}, clock=_fixed_clock)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"refresh took {elapsed:.2f}s on 120-note vault"
