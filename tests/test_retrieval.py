"""Tests for ``kernel.retrieval.gather_context`` — minimal tier order.

The full eight-Boolean wiring lives in issue #10. Issue #3 only needs the
**minimal tier order**:

    INDEX.md  ->  active_session.md  ->  grep over vault/<domain>/
                                    ->   read top-N matched files

The bundle returned is the ordered list of snippets the agent will see,
plus the file paths consulted (so the orchestrator can audit them and the
journal handler can cite them in the reply).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kernel.retrieval import ContextBundle, gather_context


# -- helpers --------------------------------------------------------------


def _seed_index(vault_root: Path, body: str = "# INDEX\n\nconsciousness, qualia\n") -> Path:
    target = vault_root / "_index" / "INDEX.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _seed_session(vault_root: Path, body: str = "session note: thinking about consciousness\n") -> Path:
    target = vault_root / "_index" / "active_session.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _seed_journal(vault_root: Path, name: str, body: str) -> Path:
    target = vault_root / "journal" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _config(*, budget: int = 6000, max_files: int = 3) -> dict:
    """A minimal retrieval-config slice the function reads from."""
    return {
        "retrieval": {
            "context_token_budget": budget,
            "max_files": max_files,
        }
    }


# -- tests ---------------------------------------------------------------


def test_gather_context_reads_index_first_when_present(tmp_path: Path) -> None:
    """The INDEX.md content must be the first snippet in the bundle when it exists."""
    vault_root = tmp_path / "vault"
    _seed_index(vault_root, body="# INDEX\n\ntopic: consciousness\n")
    _seed_session(vault_root)
    _seed_journal(
        vault_root,
        "2026-04-01-consciousness-note.md",
        "thinking deeply about consciousness today",
    )

    bundle = gather_context(
        query="what did I think about consciousness?",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    assert isinstance(bundle, ContextBundle)
    assert len(bundle.snippets) >= 1
    assert "topic: consciousness" in bundle.snippets[0].text
    assert bundle.snippets[0].source.endswith("INDEX.md")


def test_gather_context_reads_session_after_index(tmp_path: Path) -> None:
    """Session snippet appears after INDEX in the bundle's tier order."""
    vault_root = tmp_path / "vault"
    _seed_index(vault_root)
    _seed_session(vault_root, body="session: consciousness research thread\n")
    _seed_journal(
        vault_root,
        "2026-04-02-thoughts.md",
        "consciousness is the topic again",
    )

    bundle = gather_context(
        query="consciousness",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    sources = [s.source for s in bundle.snippets]
    # INDEX precedes active_session.
    index_idx = next(i for i, s in enumerate(sources) if s.endswith("INDEX.md"))
    session_idx = next(i for i, s in enumerate(sources) if s.endswith("active_session.md"))
    assert index_idx < session_idx


def test_gather_context_greps_domain_dir_for_query_terms(tmp_path: Path) -> None:
    """Files in vault/<domain>/ matching query terms get read into the bundle."""
    vault_root = tmp_path / "vault"
    _seed_index(vault_root)
    _seed_session(vault_root)

    matched = _seed_journal(
        vault_root,
        "2026-04-15-memgpt-tiered-memory.md",
        "memgpt is a tiered memory architecture for agents",
    )
    _seed_journal(
        vault_root,
        "2026-04-16-unrelated.md",
        "today I went to the grocery store",
    )

    bundle = gather_context(
        query="memgpt tiered memory",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    matched_sources = [s.source for s in bundle.snippets]
    assert any(s.endswith(matched.name) for s in matched_sources)
    # The unrelated note is NOT read — grep filtered it out.
    assert not any(s.endswith("2026-04-16-unrelated.md") for s in matched_sources)


def test_gather_context_returns_consulted_paths_for_audit(tmp_path: Path) -> None:
    """The bundle exposes a flat list of file paths so the orchestrator can audit them."""
    vault_root = tmp_path / "vault"
    _seed_index(vault_root)
    _seed_session(vault_root)
    matched = _seed_journal(
        vault_root,
        "2026-04-17-coffee.md",
        "i drink too much coffee on weekdays",
    )

    bundle = gather_context(
        query="coffee",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    paths_str = [str(p) for p in bundle.paths]
    assert any(str(matched) == p for p in paths_str)


def test_gather_context_respects_token_budget(tmp_path: Path) -> None:
    """Total snippet text length stays within the configured budget (char proxy)."""
    vault_root = tmp_path / "vault"
    _seed_index(vault_root, body="x" * 200)
    _seed_session(vault_root, body="y" * 200)

    # Three large journal files; budget is small so not all should fit.
    big_body = "consciousness " * 200
    for i in range(3):
        _seed_journal(vault_root, f"2026-04-2{i}-conscious-{i}.md", big_body)

    bundle = gather_context(
        query="consciousness",
        config=_config(budget=600, max_files=3),
        vault_root=vault_root,
        domain="journal",
    )

    total_chars = sum(len(s.text) for s in bundle.snippets)
    # Budget is rough — allow up to 4x for a char proxy on token budget.
    # The point: some material was elided so we did not blow past the cap.
    assert total_chars <= 600 * 4


def test_gather_context_caps_files_at_max_files(tmp_path: Path) -> None:
    """At most ``max_files`` matching files are read into the bundle."""
    vault_root = tmp_path / "vault"
    _seed_index(vault_root)
    _seed_session(vault_root)

    for i in range(5):
        _seed_journal(
            vault_root,
            f"2026-04-1{i}-conscious-{i}.md",
            "consciousness consciousness consciousness",
        )

    bundle = gather_context(
        query="consciousness",
        config=_config(max_files=2),
        vault_root=vault_root,
        domain="journal",
    )

    # Only the journal-tier snippets count toward max_files.
    journal_snippets = [
        s for s in bundle.snippets if "/journal/" in s.source
    ]
    assert len(journal_snippets) <= 2


def test_gather_context_degrades_gracefully_when_index_missing(tmp_path: Path) -> None:
    """Missing INDEX.md is not an error — bundle just lacks the index snippet."""
    vault_root = tmp_path / "vault"
    # No INDEX, no session.
    _seed_journal(
        vault_root,
        "2026-04-22-orphan.md",
        "a thought without an index",
    )

    bundle = gather_context(
        query="thought",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    # No INDEX snippet, no session snippet — but journal grep still works.
    sources = [s.source for s in bundle.snippets]
    assert not any(s.endswith("INDEX.md") for s in sources)
    assert not any(s.endswith("active_session.md") for s in sources)
    assert any(s.endswith("2026-04-22-orphan.md") for s in sources)


def test_gather_context_returns_empty_bundle_when_vault_missing(tmp_path: Path) -> None:
    """A vault path that doesn't exist yields an empty bundle, not an error."""
    bundle = gather_context(
        query="anything",
        config=_config(),
        vault_root=tmp_path / "no-such-vault",
        domain="journal",
    )

    assert isinstance(bundle, ContextBundle)
    assert bundle.snippets == ()
    assert bundle.paths == ()
