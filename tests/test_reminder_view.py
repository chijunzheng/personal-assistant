"""Tests for ``domains.reminder.view`` — projected markdown view.

The view is a one-way projection from ``vault/reminder/events.jsonl`` to
``vault/reminder/reminders.md`` for Obsidian visibility. The source of
truth remains the JSONL event log (append-only, sha256 ids per
``kernel/SYNC.md``); the markdown view is regenerated on every write
and never read back.

Slice 1 (issue #32): pending reminders only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from domains.reminder.view import render


def _fixed_now() -> datetime:
    return datetime(2026, 5, 11, 15, 57, 0, tzinfo=timezone.utc)


def _write_events(path: Path, rows: list[dict]) -> None:
    """Helper — write a synthetic events.jsonl fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# ---------------------------------------------------------------------------
# Cycle 1 — empty log
# ---------------------------------------------------------------------------


def test_render_empty_log_produces_header_only(tmp_path: Path) -> None:
    """No events on disk -> a minimal valid .md with header + timestamp, no Pending."""
    events_path = tmp_path / "events.jsonl"
    # Note: file does not exist yet — render() must tolerate that.

    output = render(events_path, now=_fixed_now())

    assert output.startswith("# Reminders\n")
    assert "Last updated: 2026-05-11T15:57:00+00:00" in output
    assert "## Pending" not in output
    assert "## Fired" not in output
    assert "## Cancelled" not in output


# ---------------------------------------------------------------------------
# Cycle 2 — scheduled pending reminder
# ---------------------------------------------------------------------------


def test_render_scheduled_pending_row(tmp_path: Path) -> None:
    """A scheduled pending reminder appears as a checkbox row under ## Pending."""
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            {
                "id": "a3f8c2" + "0" * 58,  # 64-hex sha256; only first 6 shown
                "kind": "scheduled",
                "status": "pending",
                "message": "Order IKEA Hemnes for Tracy",
                "fire_at": "2026-05-17T18:00:00+00:00",
                "created_at": "2026-05-11T15:57:00+00:00",
            }
        ],
    )

    output = render(events_path, now=_fixed_now())

    assert "## Pending" in output
    assert "- [ ] Order IKEA Hemnes for Tracy" in output
    assert "2026-05-17T18:00:00+00:00" in output
    assert "<!-- id: a3f8c2 | scheduled -->" in output


# ---------------------------------------------------------------------------
# Cycle 3 — state-derived pending row shows the condition
# ---------------------------------------------------------------------------


def test_render_state_derived_pending_row_shows_condition(tmp_path: Path) -> None:
    """An add_when reminder renders with 'when <condition>' visible in the line."""
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            {
                "id": "c9e201" + "0" * 58,
                "kind": "state_derived",
                "status": "pending",
                "message": "buy AAA batteries",
                "condition": "inventory.low?item=AAA batteries",
                "check_interval_min": 5,
                "created_at": "2026-05-11T15:57:00+00:00",
            }
        ],
    )

    output = render(events_path, now=_fixed_now())

    assert "- [ ] buy AAA batteries" in output
    assert "when inventory.low?item=AAA batteries" in output
    assert "<!-- id: c9e201 | state_derived -->" in output


# ---------------------------------------------------------------------------
# Cycle 4 — retired reminders (cancelled / fired) do NOT appear in Pending
# ---------------------------------------------------------------------------


def test_render_excludes_cancelled_and_fired_from_pending(tmp_path: Path) -> None:
    """Cancel events (via target_id) and fired-status rows are absent from Pending."""
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            # 1) Still pending — should appear.
            {
                "id": "aaaa11" + "0" * 58,
                "kind": "scheduled",
                "status": "pending",
                "message": "still pending",
                "fire_at": "2026-05-17T18:00:00+00:00",
            },
            # 2) Later cancelled — bookkeeping cancel event points at it.
            {
                "id": "bbbb22" + "0" * 58,
                "kind": "scheduled",
                "status": "pending",
                "message": "to be cancelled",
                "fire_at": "2026-05-18T18:00:00+00:00",
            },
            {
                "id": "cancel-ev-1",
                "kind": "cancel",
                "status": "cancelled",
                "target_id": "bbbb22" + "0" * 58,
                "created_at": "2026-05-11T16:00:00+00:00",
            },
            # 3) Fired — status flipped to 'fired'.
            {
                "id": "cccc33" + "0" * 58,
                "kind": "scheduled",
                "status": "fired",
                "message": "already fired",
                "fire_at": "2026-05-10T12:00:00+00:00",
                "fired_at": "2026-05-10T12:00:00+00:00",
            },
        ],
    )

    output = render(events_path, now=_fixed_now())

    assert "- [ ] still pending" in output
    assert "to be cancelled" not in output
    assert "already fired" not in output


# ---------------------------------------------------------------------------
# Cycle 5 — multiple pending reminders preserve event-log insertion order
# ---------------------------------------------------------------------------


def test_render_preserves_insertion_order(tmp_path: Path) -> None:
    """Multiple pending rows appear in the same order as the event log."""
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            {
                "id": "aaa111" + "0" * 58,
                "kind": "scheduled",
                "status": "pending",
                "message": "first one",
                "fire_at": "2026-05-12T08:00:00+00:00",
            },
            {
                "id": "bbb222" + "0" * 58,
                "kind": "scheduled",
                "status": "pending",
                "message": "second one",
                "fire_at": "2026-05-13T08:00:00+00:00",
            },
            {
                "id": "ccc333" + "0" * 58,
                "kind": "scheduled",
                "status": "pending",
                "message": "third one",
                "fire_at": "2026-05-14T08:00:00+00:00",
            },
        ],
    )

    output = render(events_path, now=_fixed_now())
    first_idx = output.index("first one")
    second_idx = output.index("second one")
    third_idx = output.index("third one")
    assert first_idx < second_idx < third_idx


# ---------------------------------------------------------------------------
# Cycle 6 — pure function: same inputs produce byte-identical output
# ---------------------------------------------------------------------------


def test_render_is_idempotent(tmp_path: Path) -> None:
    """Calling render() twice with the same inputs produces identical bytes."""
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            {
                "id": "aaa111" + "0" * 58,
                "kind": "scheduled",
                "status": "pending",
                "message": "stable reminder",
                "fire_at": "2026-05-12T08:00:00+00:00",
            },
            {
                "id": "bbb222" + "0" * 58,
                "kind": "state_derived",
                "status": "pending",
                "message": "another stable one",
                "condition": "inventory.low?item=milk",
                "check_interval_min": 5,
            },
        ],
    )

    first = render(events_path, now=_fixed_now())
    second = render(events_path, now=_fixed_now())
    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")
