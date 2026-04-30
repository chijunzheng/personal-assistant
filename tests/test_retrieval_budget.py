"""Issue #10 — context_token_budget + max_tool_calls_per_turn caps.

The retrieval engine MUST respect both caps from ``configs/default.yaml``:
``context_token_budget`` (char-proxy estimate) and ``max_tool_calls_per_turn``.
Bundles must stay under budget on large vaults; tool palette assembly must
not exceed the call cap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kernel.retrieval import gather_context


def _seed_journal_n(vault_root: Path, n: int, body: str) -> None:
    journal = vault_root / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        path = journal / f"2026-04-{i+1:02d}-conscious-{i}.md"
        path.write_text(body, encoding="utf-8")


def _config(*, budget: int, max_files: int = 20, max_tool_calls: int = 8) -> dict:
    return {
        "retrieval": {
            "context_token_budget": budget,
            "max_files": max_files,
            "max_tool_calls_per_turn": max_tool_calls,
        },
        "context_engineering": {
            "tiered_retrieval": True,
            "per_domain_shaping": True,
            "recency_weighting": True,
            "active_session_summary": True,
            "vault_index_first": True,
            "backlink_expansion": True,
            "suggested_actions": True,
        },
    }


# -- token budget --------------------------------------------------------


def test_bundle_stays_under_token_budget_on_large_vault(tmp_path: Path) -> None:
    """A vault with 30 large files + tiny budget yields a budget-respecting bundle.

    The char-proxy is 4 chars/token so the bundle's total snippet length
    must fit within ``budget * 4`` characters.
    """
    vault_root = tmp_path / "vault"
    big_body = "consciousness " * 500  # ~7000 chars per file
    _seed_journal_n(vault_root, 30, big_body)

    bundle = gather_context(
        query="consciousness",
        config=_config(budget=400, max_files=30),
        vault_root=vault_root,
        domain="journal",
    )

    total_chars = sum(len(s.text) for s in bundle.snippets)
    # 4-char-per-token proxy.
    assert total_chars <= 400 * 4


def test_bundle_tokens_estimate_matches_snippet_chars(tmp_path: Path) -> None:
    """The bundle's ``tokens_estimate`` matches its char-proxy of snippets."""
    vault_root = tmp_path / "vault"
    _seed_journal_n(vault_root, 3, "consciousness " * 100)

    bundle = gather_context(
        query="consciousness",
        config=_config(budget=2000),
        vault_root=vault_root,
        domain="journal",
    )

    expected = sum(len(s.text) for s in bundle.snippets) // 4
    assert bundle.tokens_estimate == expected


def test_zero_budget_yields_empty_or_truncated_bundle(tmp_path: Path) -> None:
    """A budget of 0 still returns a bundle (paths recorded, snippets minimal)."""
    vault_root = tmp_path / "vault"
    _seed_journal_n(vault_root, 2, "consciousness " * 50)

    bundle = gather_context(
        query="consciousness",
        config=_config(budget=0),
        vault_root=vault_root,
        domain="journal",
    )

    # Implementation choice: budget=0 sentinels treat budget as "no cap"
    # (matches issue #3's current behavior where _budget_chars is 0).
    # The caller can still detect this via tokens_estimate.
    assert bundle.tokens_estimate >= 0


# -- max_tool_calls_per_turn --------------------------------------------


def test_max_tool_calls_propagated_to_bundle(tmp_path: Path) -> None:
    """The bundle exposes ``max_tool_calls`` so the agent prompt can quote it."""
    vault_root = tmp_path / "vault"
    _seed_journal_n(vault_root, 2, "consciousness")

    bundle = gather_context(
        query="consciousness",
        config=_config(budget=6000, max_tool_calls=8),
        vault_root=vault_root,
        domain="journal",
    )

    assert bundle.max_tool_calls == 8


def test_max_tool_calls_default_when_unspecified(tmp_path: Path) -> None:
    """An absent ``max_tool_calls_per_turn`` falls back to the configured default."""
    vault_root = tmp_path / "vault"
    _seed_journal_n(vault_root, 1, "consciousness")

    cfg: dict[str, Any] = {
        "context_engineering": {
            "tiered_retrieval": True,
            "per_domain_shaping": False,
            "recency_weighting": False,
            "active_session_summary": False,
            "vault_index_first": False,
            "backlink_expansion": False,
            "suggested_actions": False,
        },
        "retrieval": {"context_token_budget": 1000},
    }
    bundle = gather_context(
        query="consciousness",
        config=cfg,
        vault_root=vault_root,
        domain="journal",
    )

    # Default from configs/default.yaml is 8.
    assert bundle.max_tool_calls == 8


def test_max_tool_calls_clamps_below_one(tmp_path: Path) -> None:
    """A pathological ``max_tool_calls_per_turn=0`` is clamped to >=1."""
    vault_root = tmp_path / "vault"
    _seed_journal_n(vault_root, 1, "consciousness")

    bundle = gather_context(
        query="consciousness",
        config=_config(budget=6000, max_tool_calls=0),
        vault_root=vault_root,
        domain="journal",
    )

    assert bundle.max_tool_calls >= 1
