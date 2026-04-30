"""Tests for ``domains.reminder.handler`` — write + read + due_reminders.

The reminder plugin persists event rows to
``vault/reminder/events.jsonl`` with sha256 idempotency keys
``id = sha256(message|fire_at|condition)``. Two kinds:

  - ``scheduled``      -> ``fire_at`` is an ISO8601 timestamp; the
                          dispatcher fires when ``fire_at <= now``.
  - ``state_derived``  -> ``condition`` is a vault-query expression
                          (e.g., ``inventory.low?item=AAA``); the
                          dispatcher fires when the condition evaluates
                          ``True``.

Cancellation preserves append-only: a fresh row with ``status=cancelled``
is appended; existing rows are never rewritten.

The LLM-backed extractor is stubbed everywhere; tests use a fixed clock
and a temp vault.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from domains.reminder.handler import (
    ReminderWriteResult,
    due_reminders,
    read,
    reminder_id,
    write,
)
from kernel.claude_runner import ClaudeResponse
from kernel.session import Session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_session(chat_id: str = "chat-1", session_id: str = "sess-rem") -> Session:
    return Session(
        chat_id=chat_id,
        session_id=session_id,
        started_at="2026-04-29T10:00:00+00:00",
        last_updated="2026-04-29T10:00:00+00:00",
        turns=0,
        summary="",
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def _stub_extractor(payload: dict):
    """Build a pluggable extractor that returns a fixed parsed reminder dict."""

    def _extract(_message: str, _intent: str) -> dict:
        return dict(payload)

    return _extract


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _stub_invoker_returning(text: str):
    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=1,
            tokens_out=1,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


# ---------------------------------------------------------------------------
# write — reminder.add (scheduled)
# ---------------------------------------------------------------------------


def test_write_add_persists_scheduled_reminder(tmp_path: Path) -> None:
    """A reminder.add row carries kind=scheduled, fire_at, and a sha256 id."""
    vault_root = tmp_path / "vault"

    result = write(
        intent="reminder.add",
        message="remind me Sunday at 6pm to call mom",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "call mom",
                "fire_at": "2026-05-03T18:00:00+00:00",
                "recurrence": None,
            }
        ),
    )

    assert isinstance(result, ReminderWriteResult)
    events_path = vault_root / "reminder" / "events.jsonl"
    rows = _read_events(events_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "scheduled"
    assert rows[0]["status"] == "pending"
    assert rows[0]["message"] == "call mom"
    assert rows[0]["fire_at"] == "2026-05-03T18:00:00+00:00"
    assert isinstance(rows[0]["id"], str)
    assert len(rows[0]["id"]) == 64
    int(rows[0]["id"], 16)  # raises if non-hex


def test_write_add_id_is_sha256_of_message_fire_at_condition(tmp_path: Path) -> None:
    """The row's id matches the documented hash recipe exactly."""
    vault_root = tmp_path / "vault"

    write(
        intent="reminder.add",
        message="remind me Sunday at 6pm to call mom",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "call mom",
                "fire_at": "2026-05-03T18:00:00+00:00",
                "recurrence": None,
            }
        ),
    )

    expected = reminder_id(
        message="call mom",
        fire_at="2026-05-03T18:00:00+00:00",
        condition=None,
    )
    rows = _read_events(vault_root / "reminder" / "events.jsonl")
    assert rows[0]["id"] == expected


def test_write_add_is_idempotent_on_identical_inputs(tmp_path: Path) -> None:
    """Re-issuing the same reminder.add does not append a second row."""
    vault_root = tmp_path / "vault"
    extractor = _stub_extractor(
        {
            "message": "call mom",
            "fire_at": "2026-05-03T18:00:00+00:00",
            "recurrence": None,
        }
    )

    first = write(
        intent="reminder.add",
        message="remind me Sunday at 6pm to call mom",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=extractor,
    )
    second = write(
        intent="reminder.add",
        message="remind me Sunday at 6pm to call mom",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=extractor,
    )

    rows = _read_events(vault_root / "reminder" / "events.jsonl")
    assert len(rows) == 1
    assert first.appended is True
    assert second.appended is False


# ---------------------------------------------------------------------------
# write — reminder.add_when (state_derived)
# ---------------------------------------------------------------------------


def test_write_add_when_persists_state_derived_reminder(tmp_path: Path) -> None:
    """A reminder.add_when row carries kind=state_derived + condition."""
    vault_root = tmp_path / "vault"

    write(
        intent="reminder.add_when",
        message="remind me when I run out of AAA batteries",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "buy AAA batteries",
                "condition": "inventory.low?item=AAA",
                "check_interval_min": 5,
            }
        ),
    )

    rows = _read_events(vault_root / "reminder" / "events.jsonl")
    assert len(rows) == 1
    assert rows[0]["kind"] == "state_derived"
    assert rows[0]["condition"] == "inventory.low?item=AAA"
    assert rows[0]["check_interval_min"] == 5
    assert rows[0]["status"] == "pending"


def test_write_add_when_id_uses_condition_in_hash(tmp_path: Path) -> None:
    """The hash recipe uses condition (since fire_at is null for state_derived)."""
    vault_root = tmp_path / "vault"

    write(
        intent="reminder.add_when",
        message="remind me when I run out of AAA batteries",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "buy AAA batteries",
                "condition": "inventory.low?item=AAA",
                "check_interval_min": 5,
            }
        ),
    )

    expected = reminder_id(
        message="buy AAA batteries",
        fire_at=None,
        condition="inventory.low?item=AAA",
    )
    rows = _read_events(vault_root / "reminder" / "events.jsonl")
    assert rows[0]["id"] == expected


# ---------------------------------------------------------------------------
# write — reminder.cancel (append-only)
# ---------------------------------------------------------------------------


def test_write_cancel_preserves_append_only(tmp_path: Path) -> None:
    """Cancellation appends a new event; the original row is never edited."""
    vault_root = tmp_path / "vault"

    add_result = write(
        intent="reminder.add",
        message="remind me Sunday at 6pm to call mom",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "call mom",
                "fire_at": "2026-05-03T18:00:00+00:00",
                "recurrence": None,
            }
        ),
    )

    write(
        intent="reminder.cancel",
        message="cancel the call mom reminder",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor({"target_id": add_result.reminder_id}),
    )

    rows = _read_events(vault_root / "reminder" / "events.jsonl")
    # Two rows on disk: original add, then cancel — original is not rewritten.
    assert len(rows) == 2
    assert rows[0]["status"] == "pending"
    assert rows[0]["id"] == add_result.reminder_id
    # The cancellation row references the target id and itself has status=cancelled.
    assert rows[1]["status"] == "cancelled"
    assert rows[1]["target_id"] == add_result.reminder_id


# ---------------------------------------------------------------------------
# read — reminder.list
# ---------------------------------------------------------------------------


def test_read_reminder_list_returns_pending_only(tmp_path: Path) -> None:
    """Cancelled and fired reminders do not appear in the pending list."""
    vault_root = tmp_path / "vault"
    extractor_a = _stub_extractor(
        {
            "message": "call mom",
            "fire_at": "2026-05-03T18:00:00+00:00",
            "recurrence": None,
        }
    )
    extractor_b = _stub_extractor(
        {
            "message": "buy bread",
            "fire_at": "2026-05-04T09:00:00+00:00",
            "recurrence": None,
        }
    )

    a = write(
        intent="reminder.add",
        message="remind me Sunday 6pm to call mom",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=extractor_a,
    )
    write(
        intent="reminder.add",
        message="remind me Monday 9am to buy bread",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=extractor_b,
    )
    # Cancel the first.
    write(
        intent="reminder.cancel",
        message="cancel the call mom reminder",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor({"target_id": a.reminder_id}),
    )

    reply = read(
        intent="reminder.list",
        query="what reminders do I have?",
        vault_root=vault_root,
    )
    text = reply.lower()
    assert "buy bread" in text
    assert "call mom" not in text


def test_read_rejects_non_list_intent(tmp_path: Path) -> None:
    """``read`` only handles ``reminder.list`` (the kernel routes others elsewhere)."""
    vault_root = tmp_path / "vault"
    try:
        read(intent="reminder.add", query="(unused)", vault_root=vault_root)
    except ValueError:
        return
    raise AssertionError("read should reject a non-list intent")


# ---------------------------------------------------------------------------
# due_reminders — utility for the dispatcher
# ---------------------------------------------------------------------------


def test_due_reminders_returns_scheduled_past_due(tmp_path: Path) -> None:
    """A scheduled reminder whose fire_at <= now is returned as due."""
    vault_root = tmp_path / "vault"

    # Past-due (fire_at is well before now).
    write(
        intent="reminder.add",
        message="remind me yesterday to call mom",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "call mom",
                "fire_at": "2026-04-28T12:00:00+00:00",
                "recurrence": None,
            }
        ),
    )
    # Future (fire_at is well after now).
    write(
        intent="reminder.add",
        message="remind me next week to buy bread",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "buy bread",
                "fire_at": "2026-05-10T12:00:00+00:00",
                "recurrence": None,
            }
        ),
    )

    now = datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)
    due = due_reminders(now=now, vault_root=vault_root)
    messages = [r["message"] for r in due]
    assert "call mom" in messages
    assert "buy bread" not in messages


def test_due_reminders_state_derived_via_pluggable_evaluator(tmp_path: Path) -> None:
    """A state_derived reminder fires when the pluggable evaluator returns True."""
    vault_root = tmp_path / "vault"

    write(
        intent="reminder.add_when",
        message="remind me when I run out of AAA",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "buy AAA",
                "condition": "inventory.low?item=AAA",
                "check_interval_min": 5,
            }
        ),
    )
    write(
        intent="reminder.add_when",
        message="remind me when eggs are low",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "buy eggs",
                "condition": "inventory.low?item=eggs",
                "check_interval_min": 5,
            }
        ),
    )

    # Evaluator stub: only AAA's condition is True.
    def _evaluator(condition: str, *, vault_root: Path) -> bool:
        return condition == "inventory.low?item=AAA"

    now = datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)
    due = due_reminders(
        now=now, vault_root=vault_root, condition_evaluator=_evaluator
    )
    messages = [r["message"] for r in due]
    assert "buy AAA" in messages
    assert "buy eggs" not in messages


def test_due_reminders_excludes_cancelled(tmp_path: Path) -> None:
    """A cancelled scheduled reminder is not surfaced as due."""
    vault_root = tmp_path / "vault"

    add_res = write(
        intent="reminder.add",
        message="remind me yesterday to call mom",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "call mom",
                "fire_at": "2026-04-28T12:00:00+00:00",
                "recurrence": None,
            }
        ),
    )
    write(
        intent="reminder.cancel",
        message="cancel the call mom reminder",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor({"target_id": add_res.reminder_id}),
    )

    now = datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)
    due = due_reminders(now=now, vault_root=vault_root)
    assert due == []


def test_due_reminders_excludes_already_fired(tmp_path: Path) -> None:
    """A reminder whose status was advanced to ``fired`` is not re-fired."""
    vault_root = tmp_path / "vault"

    write(
        intent="reminder.add",
        message="remind me yesterday to call mom",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "message": "call mom",
                "fire_at": "2026-04-28T12:00:00+00:00",
                "recurrence": None,
            }
        ),
    )
    # Append a fire event referencing the original reminder.
    rows = _read_events(vault_root / "reminder" / "events.jsonl")
    target_id = rows[0]["id"]
    extra = {
        "id": "fire-" + target_id,
        "kind": "scheduled",
        "status": "fired",
        "target_id": target_id,
        "message": "call mom",
        "fire_at": "2026-04-28T12:00:00+00:00",
        "fired_at": "2026-04-29T12:30:00+00:00",
    }
    events_path = vault_root / "reminder" / "events.jsonl"
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(extra) + "\n")

    now = datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)
    due = due_reminders(now=now, vault_root=vault_root)
    assert due == []


def test_write_rejects_unknown_intent(tmp_path: Path) -> None:
    """write() only handles add | add_when | cancel."""
    vault_root = tmp_path / "vault"
    try:
        write(
            intent="reminder.list",
            message="(unused)",
            session=_make_session(),
            vault_root=vault_root,
            clock=_fixed_clock,
            extractor=_stub_extractor({"message": "x"}),
        )
    except ValueError:
        return
    raise AssertionError("write should reject reminder.list")


def test_read_reminder_list_default_when_empty(tmp_path: Path) -> None:
    """Empty reminder log yields a friendly 'no reminders' reply."""
    vault_root = tmp_path / "vault"
    reply = read(
        intent="reminder.list", query="what reminders do I have?", vault_root=vault_root
    )
    assert "no" in reply.lower() or "0" in reply
