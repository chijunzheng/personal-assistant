"""Tests for ``domains.inventory.digest`` — daily low-stock digest contributor.

The digest's contract: given a vault root, return a markdown-friendly
string summarizing items where ``quantity < low_threshold``. When nothing
is low the function returns an empty string so the digest assembler can
omit the section cleanly.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from domains.inventory.digest import summarize


def _seed_state(vault_root: Path, state: dict) -> None:
    inventory = vault_root / "inventory"
    inventory.mkdir(parents=True, exist_ok=True)
    (inventory / "state.yaml").write_text(
        yaml.safe_dump(state, sort_keys=True), encoding="utf-8"
    )


def test_summarize_lists_low_stock_items(tmp_path: Path) -> None:
    """When some items are below threshold, they're listed in the summary."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {
            "milk": {"quantity": 0, "unit": "count", "low_threshold": 1},
            "eggs": {"quantity": 12, "unit": "count", "low_threshold": 6},
            "AAA batteries": {"quantity": 1, "unit": "count", "low_threshold": 4},
        },
    )

    summary = summarize(vault_root=vault_root)
    assert "milk" in summary.lower()
    assert "AAA batteries" in summary
    # Eggs are above threshold and must not appear.
    assert "eggs" not in summary.lower()


def test_summarize_returns_empty_when_nothing_low(tmp_path: Path) -> None:
    """Nothing below threshold -> empty string so the digest section is omitted."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {"milk": {"quantity": 4, "unit": "count", "low_threshold": 1}},
    )

    summary = summarize(vault_root=vault_root)
    assert summary == ""


def test_summarize_returns_empty_when_no_state_file(tmp_path: Path) -> None:
    """Missing state.yaml is non-fatal — return empty string."""
    vault_root = tmp_path / "vault"
    summary = summarize(vault_root=vault_root)
    assert summary == ""


def test_summarize_includes_quantity_and_threshold(tmp_path: Path) -> None:
    """Each line in the summary surfaces the current quantity + threshold."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {"milk": {"quantity": 0, "unit": "count", "low_threshold": 1}},
    )
    summary = summarize(vault_root=vault_root)
    assert "0" in summary  # current quantity
    assert "1" in summary  # threshold
