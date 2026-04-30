"""Finance weekly digest contributor — spending breakdown by category.

The digest assembler (``kernel.proactive`` weekly task) calls
``summarize(vault_root=..., since=..., until=...)`` once per scheduled
run. The function reads ``vault/finance/transactions.jsonl``, filters to
expense rows (negative amounts) inside the window, aggregates by
category, and returns a markdown-friendly breakdown.

When no spending falls inside the window, returns an empty string so
the assembler can omit the section cleanly.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

__all__ = ["summarize"]


_TRANSACTIONS_RELATIVE = Path("finance") / "transactions.jsonl"
_DEFAULT_WINDOW_DAYS = 7


def _iter_rows(path: Path) -> Iterable[dict]:
    """Yield every transaction row, skipping blank/bad lines."""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _row_date(row: dict) -> Optional[datetime]:
    """Coerce the row's ``date`` to a tz-aware datetime, or None."""
    raw = row.get("date")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _row_amount(row: dict) -> float:
    """Coerce the row's ``amount`` to float; default 0 on parse error."""
    raw = row.get("amount", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _row_currency(row: dict) -> str:
    """Best-effort currency lookup; defaults to CAD."""
    raw = row.get("currency")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "CAD"


def _format_breakdown(
    *,
    by_category: dict[str, float],
    currency: str,
) -> str:
    """Render the per-category totals as a sorted markdown bullet list."""
    if not by_category:
        return ""
    sorted_items = sorted(
        by_category.items(), key=lambda kv: kv[1]
    )  # most-negative first => biggest spend first
    lines: list[str] = []
    for category, total in sorted_items:
        # Total is negative for expenses; show the absolute value.
        lines.append(f"- {category}: ${abs(total):.2f} {currency}")
    return "## Finance: spending this week\n\n" + "\n".join(lines) + "\n"


def summarize(
    *,
    vault_root: str | os.PathLike[str],
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> str:
    """Return a markdown spending breakdown for the window ``[since, until)``.

    Args:
        vault_root: vault root on disk; missing transactions log is non-fatal.
        since: inclusive lower bound. Defaults to seven days before now.
        until: exclusive upper bound. Defaults to now.

    Returns:
        A section like::

            ## Finance: spending this week

            - groceries: $50.00 CAD
            - coffee: $8.25 CAD

        Or an empty string when no expenses fall inside the window.
    """
    txn_path = Path(vault_root) / _TRANSACTIONS_RELATIVE
    now = datetime.now(tz=timezone.utc)
    if since is None:
        since = now - timedelta(days=_DEFAULT_WINDOW_DAYS)
    if until is None:
        until = now
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    by_category: dict[str, float] = {}
    currency = "CAD"
    for row in _iter_rows(txn_path):
        amount = _row_amount(row)
        if amount >= 0:
            continue  # skip income / refunds — spending only
        date = _row_date(row)
        if date is None or date < since or date >= until:
            continue
        category = str(row.get("category") or "uncategorized")
        by_category[category] = by_category.get(category, 0.0) + amount
        currency = _row_currency(row)

    return _format_breakdown(by_category=by_category, currency=currency)
