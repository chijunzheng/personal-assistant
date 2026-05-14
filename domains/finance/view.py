"""Finance plugin — projected monthly markdown rollups.

One-way projection from ``vault/finance/transactions.jsonl`` to dated
monthly rollup files (``vault/finance/YYYY-MM.md``) so the finance domain
is browsable in Obsidian. The JSONL log stays canonical (append-only,
sha256 ids — see ``kernel/SYNC.md``); the markdown rollups are regenerated
on every write and never read back (ADR 2026-05-14).

What makes the read-back-forbidden contract safe is machine-checkable:
every rollup carries a ``generated_by:`` frontmatter marker, and both
``kernel/index.py`` and ``kernel/retrieval.py`` key off that marker to keep
generated files out of their read paths.

Each rollup also carries deterministic structural ``[[wikilinks]]`` — prev/
next month rollups and journal notes authored in the same calendar month —
which light up ``INDEX.md``'s existing backlink graph. The links are pure
functions of dates and filesystem layout; no LLM is involved.

The renderer's signature is the uniform kernel-view contract::

    def render(vault_root: Path, *, now: datetime) -> None: ...

It is a pure function of canonical storage: same JSONL in, byte-identical
markdown out. ``kernel.vault.atomic_write`` content-compares, so unchanged
months produce no Drive-sync churn.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from kernel.vault import atomic_write

__all__ = ["render"]


_TRANSACTIONS_RELATIVE = Path("finance") / "transactions.jsonl"
_FINANCE_DIR = "finance"
_JOURNAL_DIR = "journal"

# The marker every generated file carries — index.py and retrieval.py both
# match on this exact key to keep generated views out of their read paths.
_GENERATED_BY = "domains/finance/view.py"

# A journal note filename is ``YYYY-MM-DD-slug.md``; we link notes whose
# leading ``YYYY-MM`` matches the rollup's month.
_MONTH_PREFIX_LEN = len("YYYY-MM")


def _iter_rows(path: Path) -> Iterable[dict]:
    """Yield each transaction row from the JSONL log; skip blank/bad lines."""
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


def _month_of(row: Mapping[str, object]) -> str | None:
    """Extract the ``YYYY-MM`` month key from a row's ISO8601 ``date``."""
    raw = row.get("date")
    if not isinstance(raw, str) or len(raw) < _MONTH_PREFIX_LEN:
        return None
    month = raw[:_MONTH_PREFIX_LEN]
    # Cheap shape check — ``YYYY-MM`` with a dash in the middle.
    if month[4] != "-" or not month[:4].isdigit() or not month[5:].isdigit():
        return None
    return month


def _group_by_month(rows: Iterable[dict]) -> dict[str, list[dict]]:
    """Bucket rows by ``YYYY-MM``; rows with an unparseable date are dropped."""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        month = _month_of(row)
        if month is None:
            continue
        buckets.setdefault(month, []).append(row)
    return buckets


def _sort_key(row: Mapping[str, object]) -> tuple:
    """Deterministic per-row order: date, then merchant, then id.

    Order-independence is a contract: the same set of rows renders to
    byte-identical markdown regardless of their order in the JSONL.
    """
    return (
        str(row.get("date") or ""),
        str(row.get("merchant_normalized") or row.get("merchant") or ""),
        str(row.get("id") or ""),
    )


def _adjacent_month(month: str, *, delta: int) -> str:
    """Return the ``YYYY-MM`` ``delta`` months away from ``month``."""
    year = int(month[:4])
    mon = int(month[5:7])
    index = (year * 12 + (mon - 1)) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _journal_notes_for_month(vault_root: Path, month: str) -> list[str]:
    """Return sorted journal-note stems whose date prefix is in ``month``.

    Stems (no ``.md``) so they slot straight into a ``[[journal/<stem>]]``
    wikilink. Missing ``journal/`` directory yields an empty list.
    """
    journal_dir = vault_root / _JOURNAL_DIR
    if not journal_dir.is_dir():
        return []
    stems = [
        path.stem
        for path in journal_dir.glob("*.md")
        if path.stem[:_MONTH_PREFIX_LEN] == month
    ]
    return sorted(stems)


def _render_row(row: Mapping[str, object]) -> str:
    """One transaction as a markdown table row."""
    date = str(row.get("date") or "")
    merchant = str(row.get("merchant_normalized") or row.get("merchant") or "")
    amount = row.get("amount")
    amount_str = f"{amount:.2f}" if isinstance(amount, (int, float)) else str(amount)
    currency = str(row.get("currency") or "")
    category = str(row.get("category") or "uncategorized")
    sub = row.get("subcategory")
    category_cell = f"{category} / {sub}" if sub else category
    return f"| {date} | {merchant} | {amount_str} {currency} | {category_cell} |"


def _render_month(
    *,
    month: str,
    rows: list[dict],
    now: datetime,
    prev_link: str | None,
    next_link: str | None,
    journal_stems: list[str],
) -> str:
    """Compose one monthly rollup document.

    The ``generated_by:`` marker and the ``generated_at:`` timestamp live
    in frontmatter; ``generated_at`` is the only place ``now`` lands so the
    body stays a pure function of the JSONL.
    """
    ordered = sorted(rows, key=_sort_key)
    total = sum(
        r["amount"] for r in ordered if isinstance(r.get("amount"), (int, float))
    )

    lines: list[str] = [
        "---",
        f"generated_by: {_GENERATED_BY}",
        f"generated_at: {now.isoformat()}",
        f"month: {month}",
        "---",
        "",
        f"# Finance — {month}",
        "",
        "> Auto-generated from `transactions.jsonl`. Do not hand-edit.",
        "",
    ]

    # Structural navigation links — prev/next month rollups.
    nav: list[str] = []
    if prev_link is not None:
        nav.append(f"[[{prev_link}]]")
    if next_link is not None:
        nav.append(f"[[{next_link}]]")
    if nav:
        lines.append("Navigation: " + " · ".join(nav))
        lines.append("")

    lines.append(f"**{len(ordered)} transaction(s)** — net {total:.2f}")
    lines.append("")
    lines.append("| date | merchant | amount | category |")
    lines.append("|---|---|---|---|")
    lines.extend(_render_row(r) for r in ordered)

    # Cross-domain structural links — journal notes from the same month.
    if journal_stems:
        lines.append("")
        lines.append("## Related journal notes")
        lines.extend(f"- [[{_JOURNAL_DIR}/{stem}]]" for stem in journal_stems)

    return "\n".join(lines) + "\n"


def render(vault_root: str | Path, *, now: datetime) -> None:
    """Regenerate every monthly finance rollup from ``transactions.jsonl``.

    Pure function of canonical storage: the same JSONL produces
    byte-identical markdown. Writes each ``vault/finance/YYYY-MM.md``
    through :func:`kernel.vault.atomic_write` so a syncing process never
    sees a half-written file and unchanged months produce no churn.

    Args:
        vault_root: vault root on disk. A missing ``transactions.jsonl`` is
            treated as an empty log — no rollups are written, no error.
        now: reference timestamp; lands only in each file's
            ``generated_at:`` frontmatter field.
    """
    root = Path(vault_root)
    buckets = _group_by_month(_iter_rows(root / _TRANSACTIONS_RELATIVE))
    months = sorted(buckets.keys())
    month_set = set(months)

    for month in months:
        prev_candidate = _adjacent_month(month, delta=-1)
        next_candidate = _adjacent_month(month, delta=1)
        prev_link = (
            f"{_FINANCE_DIR}/{prev_candidate}"
            if prev_candidate in month_set
            else None
        )
        next_link = (
            f"{_FINANCE_DIR}/{next_candidate}"
            if next_candidate in month_set
            else None
        )
        body = _render_month(
            month=month,
            rows=buckets[month],
            now=now,
            prev_link=prev_link,
            next_link=next_link,
            journal_stems=_journal_notes_for_month(root, month),
        )
        atomic_write(root / _FINANCE_DIR / f"{month}.md", body)
