"""Tests for ``domains.reminder.view`` — projected markdown view.

The view is a one-way projection from ``vault/reminder/events.jsonl`` to
``vault/reminder/reminders.md`` for Obsidian visibility. The source of
truth remains the JSONL event log (append-only, sha256 ids per
``kernel/SYNC.md``); the markdown view is regenerated on every write
and never read back.

Issue #38 retrofits this renderer onto the kernel view primitive
(ADR 2026-05-14): the signature is now the uniform
``render(vault_root, *, now) -> None`` — the renderer reads canonical
storage itself and writes its output file through
``kernel.vault.atomic_write``. It also carries a ``generated_by:``
frontmatter marker (so ``index.py`` / ``retrieval.py`` keep it out of
their read paths) and deterministic structural ``[[wikilinks]]``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from domains.reminder.view import render


def _fixed_now() -> datetime:
    return datetime(2026, 5, 11, 15, 57, 0, tzinfo=timezone.utc)


def _write_events(vault_root: Path, rows: list[dict]) -> Path:
    """Helper — write a synthetic events.jsonl fixture, return its path."""
    events_path = vault_root / "reminder" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row) for row in rows]
    events_path.write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )
    return events_path


def _view_text(vault_root: Path) -> str:
    return (vault_root / "reminder" / "reminders.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Cycle 1 — empty log
# ---------------------------------------------------------------------------


def test_render_empty_log_produces_header_only(tmp_path: Path) -> None:
    """No events on disk -> a minimal valid .md with header, no Pending."""
    vault_root = tmp_path / "vault"
    (vault_root / "reminder").mkdir(parents=True, exist_ok=True)
    # Note: events.jsonl does not exist — render() must tolerate that.

    render(vault_root, now=_fixed_now())

    output = _view_text(vault_root)
    assert "# Reminders" in output
    assert "Last updated: 2026-05-11T15:57:00+00:00" in output
    assert "## Pending" not in output
    assert "## Fired" not in output
    assert "## Cancelled" not in output


# ---------------------------------------------------------------------------
# Cycle 2 — generated_by marker (issue #38)
# ---------------------------------------------------------------------------


def test_render_carries_generated_by_marker(tmp_path: Path) -> None:
    """reminders.md carries a ``generated_by:`` frontmatter marker."""
    vault_root = tmp_path / "vault"
    _write_events(vault_root, [])

    render(vault_root, now=_fixed_now())

    output = _view_text(vault_root)
    assert output.startswith("---\n")
    frontmatter = output.split("---", 2)[1]
    assert "generated_by:" in frontmatter
    assert "domains/reminder/view.py" in frontmatter


# ---------------------------------------------------------------------------
# Cycle 3 — scheduled pending reminder
# ---------------------------------------------------------------------------


def test_render_scheduled_pending_row(tmp_path: Path) -> None:
    """A scheduled pending reminder appears as a checkbox row under ## Pending."""
    vault_root = tmp_path / "vault"
    _write_events(
        vault_root,
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

    render(vault_root, now=_fixed_now())

    output = _view_text(vault_root)
    assert "## Pending" in output
    assert "- [ ] Order IKEA Hemnes for Tracy" in output
    assert "2026-05-17T18:00:00+00:00" in output
    assert "<!-- id: a3f8c2 | scheduled -->" in output


# ---------------------------------------------------------------------------
# Cycle 4 — state-derived pending row shows the condition
# ---------------------------------------------------------------------------


def test_render_state_derived_pending_row_shows_condition(tmp_path: Path) -> None:
    """An add_when reminder renders with 'when <condition>' visible in the line."""
    vault_root = tmp_path / "vault"
    _write_events(
        vault_root,
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

    render(vault_root, now=_fixed_now())

    output = _view_text(vault_root)
    assert "- [ ] buy AAA batteries" in output
    assert "when inventory.low?item=AAA batteries" in output
    assert "<!-- id: c9e201 | state_derived -->" in output


# ---------------------------------------------------------------------------
# Cycle 5 — retired reminders (cancelled / fired) do NOT appear in Pending
# ---------------------------------------------------------------------------


def test_render_excludes_cancelled_and_fired_from_pending(tmp_path: Path) -> None:
    """Cancel events (via target_id) and fired-status rows are absent from Pending."""
    vault_root = tmp_path / "vault"
    _write_events(
        vault_root,
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

    render(vault_root, now=_fixed_now())

    output = _view_text(vault_root)
    assert "- [ ] still pending" in output
    assert "to be cancelled" not in output
    assert "already fired" not in output


# ---------------------------------------------------------------------------
# Cycle 6 — multiple pending reminders preserve event-log insertion order
# ---------------------------------------------------------------------------


def test_render_preserves_insertion_order(tmp_path: Path) -> None:
    """Multiple pending rows appear in the same order as the event log."""
    vault_root = tmp_path / "vault"
    _write_events(
        vault_root,
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

    render(vault_root, now=_fixed_now())
    output = _view_text(vault_root)
    first_idx = output.index("first one")
    second_idx = output.index("second one")
    third_idx = output.index("third one")
    assert first_idx < second_idx < third_idx


# ---------------------------------------------------------------------------
# Cycle 7 — structural wikilinks (issue #38)
# ---------------------------------------------------------------------------


def test_render_links_same_month_journal_notes(tmp_path: Path) -> None:
    """reminders.md links journal notes authored in the same calendar month."""
    vault_root = tmp_path / "vault"
    _write_events(
        vault_root,
        [
            {
                "id": "aaa111" + "0" * 58,
                "kind": "scheduled",
                "status": "pending",
                "message": "call mom",
                "fire_at": "2026-05-20T08:00:00+00:00",
            }
        ],
    )
    journal = vault_root / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    (journal / "2026-05-09-thoughts-ab12.md").write_text(
        "# thoughts\n", encoding="utf-8"
    )
    (journal / "2026-07-01-unrelated-cd34.md").write_text(
        "# later\n", encoding="utf-8"
    )

    render(vault_root, now=_fixed_now())

    output = _view_text(vault_root)
    assert "[[journal/2026-05-09-thoughts-ab12]]" in output
    assert "2026-07-01-unrelated-cd34" not in output


# ---------------------------------------------------------------------------
# Cycle 8 — pure function: same inputs produce byte-identical output
# ---------------------------------------------------------------------------


def test_render_is_byte_deterministic(tmp_path: Path) -> None:
    """Calling render() twice with the same inputs produces identical bytes."""
    vault_root = tmp_path / "vault"
    _write_events(
        vault_root,
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

    render(vault_root, now=_fixed_now())
    first = (vault_root / "reminder" / "reminders.md").read_bytes()
    render(vault_root, now=_fixed_now())
    second = (vault_root / "reminder" / "reminders.md").read_bytes()
    assert first == second


# ---------------------------------------------------------------------------
# Cycle 9 — missing reminder/ directory is tolerated
# ---------------------------------------------------------------------------


def test_render_tolerates_missing_events_file(tmp_path: Path) -> None:
    """A missing events.jsonl is treated as an empty log; no crash."""
    vault_root = tmp_path / "vault"

    render(vault_root, now=_fixed_now())  # must not raise

    assert (vault_root / "reminder" / "reminders.md").exists()
