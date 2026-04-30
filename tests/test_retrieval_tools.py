"""Issue #10 — ``expand_keywords`` and ``read_backlinks`` tool callables.

These two helpers are exposed in the agent tool palette under engineered
config (per ``configs/default.yaml``); the tests exercise their semantics
in isolation, with a mocked LLM invoker so the suite never shells out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from kernel.claude_runner import ClaudeResponse
from kernel.retrieval import expand_keywords, read_backlinks


# -- helpers -------------------------------------------------------------


def _stub_invoker(text: str) -> Callable[..., ClaudeResponse]:
    """Build a deterministic LLM invoker that returns ``text`` once."""

    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=1,
            tokens_out=1,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def _failing_invoker(*_args: Any, **_kwargs: Any) -> ClaudeResponse:
    raise RuntimeError("LLM unreachable")


# -- expand_keywords -----------------------------------------------------


def test_expand_keywords_returns_seed_first(tmp_path: Path) -> None:
    """Seed term is always the first element of the returned list."""
    invoker = _stub_invoker("self-awareness, qualia, mind, cognition")

    out = expand_keywords("consciousness", invoker=invoker)

    assert out, "expected non-empty expansion"
    assert out[0] == "consciousness"


def test_expand_keywords_returns_synonyms(tmp_path: Path) -> None:
    """The LLM-supplied synonyms are appended after the seed."""
    invoker = _stub_invoker("self-awareness, qualia, subjective experience")

    out = expand_keywords("consciousness", invoker=invoker)

    assert "self-awareness" in out
    assert "qualia" in out
    assert "subjective experience" in out


def test_expand_keywords_lowercases_and_dedupes(tmp_path: Path) -> None:
    """Returned terms are lowercased; duplicates are dropped."""
    invoker = _stub_invoker("Self-Awareness, qualia, QUALIA, Self-Awareness")

    out = expand_keywords("Consciousness", invoker=invoker)

    # All lowercase; no duplicates.
    assert all(term == term.lower() for term in out)
    assert len(out) == len(set(out))


def test_expand_keywords_empty_seed_returns_empty(tmp_path: Path) -> None:
    """An empty seed query returns an empty list — defensive fast-path."""
    out = expand_keywords("   ", invoker=_stub_invoker("a, b, c"))
    assert out == []


def test_expand_keywords_falls_back_when_llm_fails(tmp_path: Path) -> None:
    """If the invoker raises, expand_keywords degrades to seed only."""
    out = expand_keywords("consciousness", invoker=_failing_invoker)
    assert out == ["consciousness"]


def test_expand_keywords_respects_max_terms_cap(tmp_path: Path) -> None:
    """The ``max_terms`` cap is honored even with a chatty LLM."""
    invoker = _stub_invoker("a, b, c, d, e, f, g, h, i, j, k, l")

    out = expand_keywords("consciousness", invoker=invoker, max_terms=4)

    assert len(out) <= 4
    # Seed still comes first.
    assert out[0] == "consciousness"


# -- read_backlinks -------------------------------------------------------


def _seed_wikilinked_vault(vault_root: Path) -> dict[str, Path]:
    """Build a tiny graph: A -> B, B -> C, isolated D."""
    journal = vault_root / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    a = journal / "A.md"
    b = journal / "B.md"
    c = journal / "C.md"
    d = journal / "D.md"
    a.write_text("Today I thought about [[B]] and a tangent.\n", encoding="utf-8")
    b.write_text("Following up — also [[C]] is relevant.\n", encoding="utf-8")
    c.write_text("End of the chain. No outgoing links here.\n", encoding="utf-8")
    d.write_text("An isolated note with no links.\n", encoding="utf-8")
    return {"A": a, "B": b, "C": c, "D": d}


def test_read_backlinks_one_hop_returns_direct_neighbors(tmp_path: Path) -> None:
    """A 1-hop walk from A reaches B but not C."""
    vault_root = tmp_path / "vault"
    seeded = _seed_wikilinked_vault(vault_root)

    out = read_backlinks(seeded["A"], vault_root=vault_root, max_hops=1)

    out_resolved = {p.resolve() for p in out}
    assert seeded["B"].resolve() in out_resolved
    assert seeded["C"].resolve() not in out_resolved


def test_read_backlinks_two_hops_returns_transitive_neighbors(
    tmp_path: Path,
) -> None:
    """A 2-hop walk reaches both B (direct) and C (via B)."""
    vault_root = tmp_path / "vault"
    seeded = _seed_wikilinked_vault(vault_root)

    out = read_backlinks(seeded["A"], vault_root=vault_root, max_hops=2)

    out_resolved = {p.resolve() for p in out}
    assert seeded["B"].resolve() in out_resolved
    assert seeded["C"].resolve() in out_resolved


def test_read_backlinks_zero_hops_returns_empty(tmp_path: Path) -> None:
    """``max_hops=0`` returns an empty list — no walk happens."""
    vault_root = tmp_path / "vault"
    seeded = _seed_wikilinked_vault(vault_root)

    out = read_backlinks(seeded["A"], vault_root=vault_root, max_hops=0)
    assert out == []


def test_read_backlinks_never_includes_seed(tmp_path: Path) -> None:
    """The seed file itself is never returned, even when it links back."""
    vault_root = tmp_path / "vault"
    seeded = _seed_wikilinked_vault(vault_root)
    # Add a back-edge from B to A.
    seeded["B"].write_text(
        "Following up — also [[C]] and [[A]].\n", encoding="utf-8"
    )

    out = read_backlinks(seeded["A"], vault_root=vault_root, max_hops=2)
    out_resolved = {p.resolve() for p in out}

    assert seeded["A"].resolve() not in out_resolved


def test_read_backlinks_dedupes_repeated_links(tmp_path: Path) -> None:
    """A target reached twice via different paths only appears once."""
    vault_root = tmp_path / "vault"
    seeded = _seed_wikilinked_vault(vault_root)
    # Make A link to B twice + once to C, so BFS sees C directly + via B.
    seeded["A"].write_text(
        "[[B]] and [[B]] again, plus [[C]].\n", encoding="utf-8"
    )

    out = read_backlinks(seeded["A"], vault_root=vault_root, max_hops=2)
    paths_str = [str(p.resolve()) for p in out]

    assert len(paths_str) == len(set(paths_str))


def test_read_backlinks_handles_missing_targets_gracefully(
    tmp_path: Path,
) -> None:
    """A wikilink to a non-existent file is silently skipped."""
    vault_root = tmp_path / "vault"
    journal = vault_root / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    seed = journal / "seed.md"
    seed.write_text("Linking to [[nonexistent-target]] only.\n", encoding="utf-8")

    out = read_backlinks(seed, vault_root=vault_root, max_hops=1)
    assert out == []


def test_read_backlinks_returns_empty_when_seed_missing(tmp_path: Path) -> None:
    """A missing seed path yields an empty list, not an error."""
    out = read_backlinks(
        tmp_path / "no-such-file.md",
        vault_root=tmp_path,
        max_hops=2,
    )
    assert out == []


def test_read_backlinks_max_hops_is_a_strict_cap(tmp_path: Path) -> None:
    """Past ``max_hops``, the walk stops — never hits hop+1 neighbors."""
    vault_root = tmp_path / "vault"
    journal = vault_root / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    # Build a 4-deep chain: A -> B -> C -> D.
    (journal / "A.md").write_text("[[B]]\n", encoding="utf-8")
    (journal / "B.md").write_text("[[C]]\n", encoding="utf-8")
    (journal / "C.md").write_text("[[D]]\n", encoding="utf-8")
    (journal / "D.md").write_text("end\n", encoding="utf-8")

    out = read_backlinks(journal / "A.md", vault_root=vault_root, max_hops=2)
    names = {p.name for p in out}

    assert "B.md" in names
    assert "C.md" in names
    assert "D.md" not in names
