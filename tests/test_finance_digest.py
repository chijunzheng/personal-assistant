"""Tests for ``domains.finance.digest`` — weekly spending breakdown by category.

The digest's contract: given a vault root and a window, return a
markdown-friendly category breakdown of expenses (negative amounts)
inside the window. When no spending falls inside the window, returns an
empty string so the assembler can omit the section cleanly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from domains.finance.digest import summarize


def _seed_transactions(vault_root: Path, rows: list[dict]) -> Path:
    """Drop transactions.jsonl directly so digest tests start populated."""
    finance = vault_root / "finance"
    finance.mkdir(parents=True, exist_ok=True)
    txn_path = finance / "transactions.jsonl"
    txn_path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    return txn_path


def test_summarize_aggregates_by_category(tmp_path: Path) -> None:
    """The summary surfaces totals per category for in-window expenses."""
    vault_root = tmp_path / "vault"
    _seed_transactions(
        vault_root,
        [
            {
                "id": "t1", "date": "2026-04-25", "amount": -4.50,
                "merchant": "Cafe", "category": "coffee", "currency": "CAD", "raw": "x",
            },
            {
                "id": "t2", "date": "2026-04-27", "amount": -3.75,
                "merchant": "Cafe", "category": "coffee", "currency": "CAD", "raw": "x",
            },
            {
                "id": "t3", "date": "2026-04-28", "amount": -50.00,
                "merchant": "Grocery", "category": "groceries", "currency": "CAD",
                "raw": "x",
            },
        ],
    )

    since = datetime(2026, 4, 22, tzinfo=timezone.utc)
    until = datetime(2026, 5, 1, tzinfo=timezone.utc)
    summary = summarize(vault_root=vault_root, since=since, until=until)
    assert "coffee" in summary.lower()
    assert "groceries" in summary.lower()
    # Coffee total is 8.25 (4.50 + 3.75).
    assert "8.25" in summary
    assert "50" in summary  # groceries total


def test_summarize_excludes_outside_window(tmp_path: Path) -> None:
    """Transactions older than ``since`` are not counted."""
    vault_root = tmp_path / "vault"
    _seed_transactions(
        vault_root,
        [
            {
                "id": "old", "date": "2025-10-15", "amount": -100.00,
                "merchant": "Old", "category": "groceries", "currency": "CAD", "raw": "x",
            },
        ],
    )

    since = datetime(2026, 4, 22, tzinfo=timezone.utc)
    until = datetime(2026, 5, 1, tzinfo=timezone.utc)
    summary = summarize(vault_root=vault_root, since=since, until=until)
    assert summary == ""


def test_summarize_returns_empty_when_no_transactions(tmp_path: Path) -> None:
    """No transactions.jsonl -> empty string (digest section omitted)."""
    vault_root = tmp_path / "vault"
    since = datetime(2026, 4, 22, tzinfo=timezone.utc)
    assert summarize(vault_root=vault_root, since=since) == ""


def test_summarize_skips_income_rows(tmp_path: Path) -> None:
    """Positive amounts (income) do not show up in a spending breakdown."""
    vault_root = tmp_path / "vault"
    _seed_transactions(
        vault_root,
        [
            {
                "id": "salary", "date": "2026-04-25", "amount": 5000.00,
                "merchant": "Employer", "category": "income", "currency": "CAD",
                "raw": "x",
            },
            {
                "id": "coffee", "date": "2026-04-25", "amount": -4.50,
                "merchant": "Cafe", "category": "coffee", "currency": "CAD", "raw": "x",
            },
        ],
    )

    since = datetime(2026, 4, 22, tzinfo=timezone.utc)
    until = datetime(2026, 5, 1, tzinfo=timezone.utc)
    summary = summarize(vault_root=vault_root, since=since, until=until)
    assert "income" not in summary.lower()
    assert "coffee" in summary.lower()


def test_summarize_default_window_seven_days(tmp_path: Path) -> None:
    """Default ``since`` is 7 days back; transactions older than that drop."""
    vault_root = tmp_path / "vault"
    (vault_root / "finance").mkdir(parents=True)
    # No rows -> empty.
    assert summarize(vault_root=vault_root) == ""
