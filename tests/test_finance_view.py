"""Tests for ``domains.finance.view`` — projected monthly markdown rollups.

The finance view is a one-way projection from
``vault/finance/transactions.jsonl`` to dated monthly rollup files
(``vault/finance/YYYY-MM.md``). The JSONL stays canonical; the markdown
is regenerated on every write and never read back (ADR 2026-05-14).

Contract under test:
  - byte-deterministic: same JSONL -> byte-identical .md output
  - each rollup carries a ``generated_by:`` frontmatter marker
  - each rollup carries structural ``[[wikilinks]]`` (prev/next month,
    same-month journal notes)
  - monthly granularity: transactions group into per-month files
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from domains.finance.view import render


def _fixed_now() -> datetime:
    return datetime(2026, 5, 14, 9, 0, 0, tzinfo=timezone.utc)


_TXNS = [
    {
        "id": "a1",
        "date": "2026-03-04",
        "amount": -4.75,
        "currency": "CAD",
        "merchant": "STARBUCKS #1234",
        "merchant_normalized": "Starbucks",
        "category": "food",
        "subcategory": "coffee",
        "source": "test",
        "raw": "2026-03-04 STARBUCKS #1234 CAD -4.75",
        "tags": [],
        "confidence": 0.95,
    },
    {
        "id": "a2",
        "date": "2026-03-12",
        "amount": -3.50,
        "currency": "CAD",
        "merchant": "TIM HORTONS",
        "merchant_normalized": "Tim Hortons",
        "category": "food",
        "subcategory": "coffee",
        "source": "test",
        "raw": "2026-03-12 TIM HORTONS CAD -3.50",
        "tags": [],
        "confidence": 0.92,
    },
    {
        "id": "a3",
        "date": "2026-04-02",
        "amount": -120.00,
        "currency": "CAD",
        "merchant": "LOBLAWS",
        "merchant_normalized": "Loblaws",
        "category": "groceries",
        "subcategory": None,
        "source": "test",
        "raw": "2026-04-02 LOBLAWS CAD -120.00",
        "tags": [],
        "confidence": 0.99,
    },
]


def _seed_transactions(vault_root: Path, rows: list[dict]) -> Path:
    finance = vault_root / "finance"
    finance.mkdir(parents=True, exist_ok=True)
    txn_file = finance / "transactions.jsonl"
    txn_file.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    return txn_file


# ---------------------------------------------------------------------------
# Cycle 1 — monthly granularity
# ---------------------------------------------------------------------------


def test_render_groups_transactions_into_monthly_files(tmp_path: Path) -> None:
    """Transactions spanning two months produce two dated rollup files."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root, _TXNS)

    render(vault_root, now=_fixed_now())

    assert (vault_root / "finance" / "2026-03.md").exists()
    assert (vault_root / "finance" / "2026-04.md").exists()
    # No stray files for months with no transactions.
    assert not (vault_root / "finance" / "2026-05.md").exists()


def test_render_rollup_lists_its_months_transactions(tmp_path: Path) -> None:
    """A monthly rollup names the merchants from that month only."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root, _TXNS)

    render(vault_root, now=_fixed_now())

    march = (vault_root / "finance" / "2026-03.md").read_text(encoding="utf-8")
    april = (vault_root / "finance" / "2026-04.md").read_text(encoding="utf-8")
    assert "Starbucks" in march and "Tim Hortons" in march
    assert "Loblaws" not in march
    assert "Loblaws" in april and "Starbucks" not in april


# ---------------------------------------------------------------------------
# Cycle 2 — generated_by marker
# ---------------------------------------------------------------------------


def test_render_files_carry_generated_by_marker(tmp_path: Path) -> None:
    """Every rollup carries a ``generated_by:`` frontmatter marker."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root, _TXNS)

    render(vault_root, now=_fixed_now())

    for name in ("2026-03.md", "2026-04.md"):
        text = (vault_root / "finance" / name).read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "generated_by:" in text.split("---", 2)[1]


# ---------------------------------------------------------------------------
# Cycle 3 — determinism
# ---------------------------------------------------------------------------


def test_render_is_byte_deterministic(tmp_path: Path) -> None:
    """Two renders of the same JSONL produce byte-identical output."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root, _TXNS)

    render(vault_root, now=_fixed_now())
    first = (vault_root / "finance" / "2026-03.md").read_bytes()

    render(vault_root, now=_fixed_now())
    second = (vault_root / "finance" / "2026-03.md").read_bytes()

    assert first == second


def test_render_is_order_independent(tmp_path: Path) -> None:
    """Row order in the JSONL must not change the rendered output."""
    vault_root_a = tmp_path / "a"
    vault_root_b = tmp_path / "b"
    _seed_transactions(vault_root_a, _TXNS)
    _seed_transactions(vault_root_b, list(reversed(_TXNS)))

    render(vault_root_a, now=_fixed_now())
    render(vault_root_b, now=_fixed_now())

    a = (vault_root_a / "finance" / "2026-03.md").read_bytes()
    b = (vault_root_b / "finance" / "2026-03.md").read_bytes()
    assert a == b


# ---------------------------------------------------------------------------
# Cycle 4 — structural wikilinks
# ---------------------------------------------------------------------------


def test_render_emits_prev_next_month_wikilinks(tmp_path: Path) -> None:
    """A rollup links to the adjacent months that actually have rollups."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root, _TXNS)

    render(vault_root, now=_fixed_now())

    march = (vault_root / "finance" / "2026-03.md").read_text(encoding="utf-8")
    april = (vault_root / "finance" / "2026-04.md").read_text(encoding="utf-8")
    # March's next is April; April's prev is March.
    assert "[[finance/2026-04]]" in march
    assert "[[finance/2026-03]]" in april


def test_render_links_same_month_journal_notes(tmp_path: Path) -> None:
    """A rollup links journal notes authored in the same calendar month."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root, _TXNS)
    journal = vault_root / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    (journal / "2026-03-09-budget-thoughts-ab12.md").write_text(
        "# budget thoughts\n", encoding="utf-8"
    )
    (journal / "2026-07-01-unrelated-cd34.md").write_text(
        "# later\n", encoding="utf-8"
    )

    render(vault_root, now=_fixed_now())

    march = (vault_root / "finance" / "2026-03.md").read_text(encoding="utf-8")
    assert "[[journal/2026-03-09-budget-thoughts-ab12]]" in march
    assert "2026-07-01-unrelated-cd34" not in march


# ---------------------------------------------------------------------------
# Cycle 5 — empty / missing input
# ---------------------------------------------------------------------------


def test_render_tolerates_missing_transactions_file(tmp_path: Path) -> None:
    """A missing transactions.jsonl is treated as an empty log, no crash."""
    vault_root = tmp_path / "vault"
    (vault_root / "finance").mkdir(parents=True, exist_ok=True)

    render(vault_root, now=_fixed_now())  # must not raise

    assert not list((vault_root / "finance").glob("2026-*.md"))
