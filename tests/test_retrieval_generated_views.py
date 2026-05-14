"""Tests for ``kernel.retrieval``'s exclusion of generated views.

ADR 2026-05-14, change 5: ``retrieval.py`` has a vault-wide ``rglob`` path
(``read_backlinks``) plus the per-domain grep path (``gather_context``).
Both gain an exclusion of ``generated_by:``-marked files so a generated
projection can never be read back as if it were a source note.
"""

from __future__ import annotations

from pathlib import Path

from kernel.retrieval import gather_context, read_backlinks


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_GENERATED = (
    "---\ngenerated_by: domains/finance/view.py\n---\n\n"
    "# Finance — 2026-03\n\nStarbucks coffee groceries\n"
)
_SOURCE = "---\ndate: 2026-03-09\n---\n\n# coffee notes\n\nthoughts on coffee\n"


# ---------------------------------------------------------------------------
# Cycle 1 — gather_context's grep never returns a generated view
# ---------------------------------------------------------------------------


def test_gather_context_skips_generated_views(tmp_path: Path) -> None:
    """A grep that would match a generated view returns the source note only."""
    vault_root = tmp_path / "vault"
    _write(vault_root / "finance" / "2026-03.md", _GENERATED)
    _write(vault_root / "finance" / "2026-03-09-coffee-notes.md", _SOURCE)

    bundle = gather_context(
        query="coffee",
        config={"retrieval": {}, "context_engineering": {}},
        vault_root=vault_root,
        domain="finance",
    )

    sources = [Path(s.source).name for s in bundle.snippets]
    assert "2026-03.md" not in sources
    # The genuine source note is still retrievable.
    assert "2026-03-09-coffee-notes.md" in sources


# ---------------------------------------------------------------------------
# Cycle 2 — read_backlinks never resolves a wikilink to a generated view
# ---------------------------------------------------------------------------


def test_read_backlinks_skips_generated_views(tmp_path: Path) -> None:
    """A ``[[2026-03]]`` wikilink must not resolve to the generated rollup."""
    vault_root = tmp_path / "vault"
    seed = vault_root / "journal" / "seed.md"
    _write(seed, "# seed\n\nlink to [[2026-03]]\n")
    # The only file named ``2026-03`` is the generated view.
    _write(vault_root / "finance" / "2026-03.md", _GENERATED)

    neighbors = read_backlinks(seed, vault_root=vault_root, max_hops=2)

    neighbor_names = [p.name for p in neighbors]
    assert "2026-03.md" not in neighbor_names
