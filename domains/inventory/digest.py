"""Inventory daily-digest contributor — low-stock alerts.

The digest assembler (``kernel.proactive`` in a future issue) calls
``summarize(vault_root=...)`` once per scheduled run. The function reads
``vault/inventory/state.yaml`` and returns a markdown-friendly string
listing items where ``quantity < low_threshold`` along with their current
quantity and the threshold so the user can sanity-check the alert.

When nothing is low, the function returns an empty string so the
assembler can omit the section cleanly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from domains.inventory.handler import query_inventory

__all__ = ["summarize"]


def _format_line(row: Mapping[str, object]) -> str:
    """Render one low-stock row as a bullet line."""
    item = row.get("item", "?")
    quantity = row.get("quantity", 0)
    unit = row.get("unit", "count")
    threshold = row.get("low_threshold", 1)
    location = row.get("location")

    unit_clause = "" if unit == "count" else f" {unit}"
    location_clause = f" ({location})" if location else ""
    return (
        f"- {item}: {quantity}{unit_clause} on hand "
        f"(threshold {threshold}){location_clause}"
    )


def summarize(*, vault_root: str | os.PathLike[str]) -> str:
    """Return a markdown low-stock alert section, or empty string if none.

    Args:
        vault_root: vault root on disk; missing state is non-fatal.

    Returns:
        A markdown-formatted summary like::

            ## Inventory: running low

            - milk: 0 on hand (threshold 1) (fridge)
            - AAA batteries: 1 on hand (threshold 4)

        Or an empty string when no items are below threshold.
    """
    result = query_inventory(mode="low_stock", vault_root=vault_root)
    items = result.get("items") or []
    if not items:
        return ""

    sorted_items = sorted(items, key=lambda r: str(r.get("item", "")))
    lines = [_format_line(row) for row in sorted_items]

    return "## Inventory: running low\n\n" + "\n".join(lines) + "\n"
