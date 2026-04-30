"""Reminder plugin — write, read, due_reminders.

Storage: append-only ``vault/reminder/events.jsonl`` with sha256 ids
``id = sha256(message|fire_at|condition)``. The plugin handles four
intents:

  - ``reminder.add``      — scheduled reminder (kind=scheduled, fire_at)
  - ``reminder.add_when`` — state-derived reminder (kind=state_derived,
                            condition, check_interval_min)
  - ``reminder.list``     — read path returning pending reminders as text
  - ``reminder.cancel``   — appends a cancellation event referencing the
                            original by ``target_id`` (preserves
                            append-only — original row is never edited)

The dispatcher utility ``due_reminders(now, vault_root, condition_evaluator)``
returns every pending reminder whose fire condition is satisfied. The
``condition_evaluator`` is pluggable so tests can stub a deterministic
evaluator and production wires a thin adapter against
``domains.inventory.handler.query_inventory`` (and friends).

The plugin is log-silent (CLAUDE.md): it never writes to ``vault/_audit``.
The kernel writes audit entries after ``write``/``read`` returns. All
appends route through ``kernel.vault.atomic_write``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from kernel.claude_runner import ClaudeResponse, invoke as claude_invoke
from kernel.session import Session
from kernel.vault import atomic_write

__all__ = [
    "ReminderWriteResult",
    "due_reminders",
    "read",
    "reminder_id",
    "write",
]


# Path inside the vault.
_EVENTS_RELATIVE = Path("reminder") / "events.jsonl"

_WRITE_INTENTS = ("reminder.add", "reminder.add_when", "reminder.cancel")
_READ_INTENTS = ("reminder.list",)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReminderWriteResult:
    """Return value of ``write`` — what the kernel needs to audit-log + reply.

    Attributes:
        intent: registered intent label.
        path: canonical events JSONL path.
        reminder_id: sha256 id of the reminder row this call wrote (the
            ``add``/``add_when`` row's id, or the cancel-event row's id).
        target_id: for ``reminder.cancel`` only — the id of the original
            reminder being cancelled. Empty string for add/add_when.
        appended: ``True`` if a new row landed on disk, ``False`` for an
            idempotent no-op.
        kind: ``scheduled`` | ``state_derived`` | ``cancel``.
    """

    intent: str
    path: Path
    reminder_id: str
    target_id: str
    appended: bool
    kind: str


# ---------------------------------------------------------------------------
# Pluggable callable shapes
# ---------------------------------------------------------------------------


class _Extractor(Protocol):
    """Maps (message, intent) -> a parsed reminder dict.

    For ``reminder.add``: returns ``message``, ``fire_at`` (ISO8601),
    optional ``recurrence``.

    For ``reminder.add_when``: returns ``message``, ``condition`` (string),
    ``check_interval_min`` (number).

    For ``reminder.cancel``: returns ``target_id`` (string id of the
    reminder to cancel).
    """

    def __call__(self, message: str, intent: str) -> Mapping[str, Any]: ...


class _ClaudeInvoker(Protocol):
    """The subset of ``claude_runner.invoke`` the handler uses."""

    def __call__(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> ClaudeResponse: ...


class _ConditionEvaluator(Protocol):
    """Evaluates a vault-query expression to a Boolean.

    Production: a thin adapter that recognizes ``inventory.low?item=X``,
    ``finance.spent_over?...``, etc. and returns whether the condition
    holds against the current vault state. Tests inject a deterministic
    stub.
    """

    def __call__(self, condition: str, *, vault_root: Path) -> bool: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def reminder_id(
    *,
    message: str,
    fire_at: Optional[str],
    condition: Optional[str],
) -> str:
    """Stable sha256 over ``message|fire_at|condition`` (the docs' recipe)."""
    serialized = f"{message}|{fire_at or ''}|{condition or ''}"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _now(clock: Optional[Callable[[], datetime]] = None) -> datetime:
    return (clock or (lambda: datetime.now(tz=timezone.utc)))()


def _iter_events(path: Path) -> Iterable[dict]:
    """Yield each event row from the JSONL log; skip blank/bad lines."""
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


def _existing_ids(path: Path) -> set[str]:
    """Return every existing event id; tolerate a missing file."""
    seen: set[str] = set()
    for row in _iter_events(path):
        rid = row.get("id")
        if isinstance(rid, str):
            seen.add(rid)
    return seen


def _append_event(path: Path, event: Mapping[str, Any]) -> None:
    """Append one event row to the JSONL via the atomic-write seam."""
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing = existing + "\n"
    line = json.dumps(dict(event), default=str) + "\n"
    atomic_write(path, existing + line)


# ---------------------------------------------------------------------------
# Default LLM-backed extractor (shells to claude_runner)
# ---------------------------------------------------------------------------


_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_prompt() -> str:
    """Read the reminder prompt; fall back to empty if absent (tests)."""
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _parse_json_payload(text: str) -> dict:
    """Decode an LLM response, tolerating optional ```code-fences```."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [ln for ln in cleaned.splitlines() if not ln.startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _default_extractor(invoker: _ClaudeInvoker) -> _Extractor:
    """Build an extractor backed by the LLM via ``claude_runner.invoke``."""
    system = _load_prompt()

    def _extract(message: str, intent: str) -> dict:
        prompt = (
            f"Intent: {intent}\n"
            "Parse this message into a JSON object describing the reminder. "
            "For reminder.add: keys 'message' (string), 'fire_at' (ISO8601), "
            "optional 'recurrence'. "
            "For reminder.add_when: keys 'message' (string), 'condition' "
            "(vault-query expression), 'check_interval_min' (number). "
            "For reminder.cancel: 'target_id' (sha256 id of the reminder "
            "to cancel). Output JSON only.\n\n"
            f"Message: {message}\n"
        )
        response = invoker(prompt, system_prompt=system or None)
        return _parse_json_payload(response.text)

    return _extract


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _build_add_event(
    *,
    parsed: Mapping[str, Any],
    intent: str,
    timestamp: str,
    session_id: str,
) -> dict:
    """Shape one extractor row into a persisted ``reminder.add`` event."""
    message = str(parsed.get("message") or "").strip()
    fire_at = str(parsed.get("fire_at") or "").strip()
    if not message:
        raise ValueError("reminder.add extractor must supply a non-empty message")
    if not fire_at:
        raise ValueError("reminder.add extractor must supply fire_at (ISO8601)")
    rid = reminder_id(message=message, fire_at=fire_at, condition=None)
    return {
        "id": rid,
        "kind": "scheduled",
        "status": "pending",
        "message": message,
        "fire_at": fire_at,
        "recurrence": parsed.get("recurrence"),
        "created_at": timestamp,
        "source": "telegram",
        "session_id": session_id,
    }


def _build_add_when_event(
    *,
    parsed: Mapping[str, Any],
    intent: str,
    timestamp: str,
    session_id: str,
) -> dict:
    """Shape one extractor row into a persisted ``reminder.add_when`` event."""
    message = str(parsed.get("message") or "").strip()
    condition = str(parsed.get("condition") or "").strip()
    if not message:
        raise ValueError(
            "reminder.add_when extractor must supply a non-empty message"
        )
    if not condition:
        raise ValueError("reminder.add_when extractor must supply a condition")
    check_interval = parsed.get("check_interval_min", 5)
    rid = reminder_id(message=message, fire_at=None, condition=condition)
    return {
        "id": rid,
        "kind": "state_derived",
        "status": "pending",
        "message": message,
        "condition": condition,
        "check_interval_min": check_interval,
        "created_at": timestamp,
        "source": "telegram",
        "session_id": session_id,
    }


def _build_cancel_event(
    *,
    parsed: Mapping[str, Any],
    timestamp: str,
    session_id: str,
) -> dict:
    """Shape a cancel event row referencing the original by ``target_id``."""
    target_id = str(parsed.get("target_id") or "").strip()
    if not target_id:
        raise ValueError(
            "reminder.cancel extractor must supply target_id of the reminder to cancel"
        )
    cancel_id = hashlib.sha256(
        f"cancel|{target_id}|{timestamp}".encode("utf-8")
    ).hexdigest()
    return {
        "id": cancel_id,
        "kind": "cancel",
        "status": "cancelled",
        "target_id": target_id,
        "created_at": timestamp,
        "source": "telegram",
        "session_id": session_id,
    }


def write(
    *,
    intent: str,
    message: str,
    session: Session,
    vault_root: str | os.PathLike[str],
    clock: Optional[Callable[[], datetime]] = None,
    extractor: Optional[_Extractor] = None,
    invoker: Optional[_ClaudeInvoker] = None,
) -> ReminderWriteResult:
    """Persist one reminder event (add, add_when, or cancel).

    Args:
        intent: one of ``reminder.add``, ``reminder.add_when``,
            ``reminder.cancel``.
        message: the user's verbatim natural-language event text.
        session: active session — supplies ``session_id`` for the event row.
        vault_root: vault root on disk.
        clock: pluggable clock (test seam).
        extractor: pluggable parser ``(message, intent) -> dict``. Defaults
            to a ``claude_runner``-backed extractor.
        invoker: passed to the default extractor when ``extractor`` is omitted.

    Returns:
        ``ReminderWriteResult`` with the row's id and whether a new row landed.

    Raises:
        ValueError: ``intent`` is not a registered write intent, or the
            extractor returned an incomplete payload.
    """
    if intent not in _WRITE_INTENTS:
        raise ValueError(
            f"reminder.write only handles {_WRITE_INTENTS}, not {intent!r}"
        )
    if not message or not message.strip():
        raise ValueError("reminder write requires a non-empty message")

    extract = extractor or _default_extractor(invoker or claude_invoke)
    parsed = dict(extract(message, intent) or {})

    timestamp = _now(clock).isoformat()
    events_path = Path(vault_root) / _EVENTS_RELATIVE

    if intent == "reminder.add":
        event_row = _build_add_event(
            parsed=parsed,
            intent=intent,
            timestamp=timestamp,
            session_id=session.session_id,
        )
        kind = "scheduled"
        target_id = ""
    elif intent == "reminder.add_when":
        event_row = _build_add_when_event(
            parsed=parsed,
            intent=intent,
            timestamp=timestamp,
            session_id=session.session_id,
        )
        kind = "state_derived"
        target_id = ""
    else:  # reminder.cancel
        event_row = _build_cancel_event(
            parsed=parsed,
            timestamp=timestamp,
            session_id=session.session_id,
        )
        kind = "cancel"
        target_id = event_row["target_id"]

    seen = _existing_ids(events_path)
    appended = False
    if event_row["id"] not in seen:
        _append_event(events_path, event_row)
        appended = True

    return ReminderWriteResult(
        intent=intent,
        path=events_path,
        reminder_id=event_row["id"],
        target_id=target_id,
        appended=appended,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# read — reminder.list
# ---------------------------------------------------------------------------


def _pending_reminders(events_path: Path) -> list[dict]:
    """Return reminders that are still pending (not cancelled, not fired)."""
    rows = list(_iter_events(events_path))
    if not rows:
        return []

    # Build a set of reminder ids that have been retired (cancelled or fired).
    retired: set[str] = set()
    for row in rows:
        if row.get("kind") == "cancel":
            target = row.get("target_id")
            if isinstance(target, str):
                retired.add(target)
        elif row.get("status") == "fired":
            target = row.get("target_id") or row.get("id")
            if isinstance(target, str):
                retired.add(target)
        elif row.get("status") == "cancelled":
            # A row that itself carries status=cancelled retires itself.
            rid = row.get("id")
            if isinstance(rid, str):
                retired.add(rid)

    pending: list[dict] = []
    for row in rows:
        if row.get("kind") == "cancel":
            continue  # cancel events themselves are bookkeeping only
        if row.get("status") != "pending":
            continue
        rid = row.get("id")
        if isinstance(rid, str) and rid in retired:
            continue
        pending.append(row)

    return pending


def _format_reminder_line(row: Mapping[str, Any]) -> str:
    """One pending reminder as a single descriptive line."""
    msg = str(row.get("message") or "").strip()
    if row.get("kind") == "scheduled":
        when = str(row.get("fire_at") or "").strip()
        return f"- {msg} (at {when})" if when else f"- {msg}"
    if row.get("kind") == "state_derived":
        cond = str(row.get("condition") or "").strip()
        return f"- {msg} (when {cond})" if cond else f"- {msg}"
    return f"- {msg}"


def read(
    *,
    intent: str,
    query: str,
    vault_root: str | os.PathLike[str],
) -> str:
    """Answer ``reminder.list`` with a textual list of pending reminders.

    Args:
        intent: must be ``reminder.list``.
        query: the user's verbatim question (unused — list is unconditional).
        vault_root: vault root on disk.

    Returns:
        A reply string the kernel can send back over Telegram.

    Raises:
        ValueError: ``intent`` is not ``reminder.list``.
    """
    if intent not in _READ_INTENTS:
        raise ValueError(
            f"reminder.read only handles {_READ_INTENTS}, not {intent!r}"
        )
    del query  # the list is unconditional in v1

    events_path = Path(vault_root) / _EVENTS_RELATIVE
    pending = _pending_reminders(events_path)
    if not pending:
        return "No pending reminders."

    lines = [_format_reminder_line(row) for row in pending]
    return "Pending reminders:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# due_reminders — utility for the dispatcher
# ---------------------------------------------------------------------------


def _is_scheduled_due(row: Mapping[str, Any], now: datetime) -> bool:
    """Return True when a scheduled reminder's fire_at <= now."""
    fire_at_raw = row.get("fire_at")
    if not isinstance(fire_at_raw, str) or not fire_at_raw.strip():
        return False
    try:
        fire_at = datetime.fromisoformat(fire_at_raw)
    except ValueError:
        return False
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return fire_at <= now


def due_reminders(
    *,
    now: Optional[datetime] = None,
    vault_root: str | os.PathLike[str],
    condition_evaluator: Optional[_ConditionEvaluator] = None,
) -> list[dict]:
    """Return reminders whose fire condition is satisfied at ``now``.

    Args:
        now: the reference time. Defaults to ``datetime.now(tz=UTC)``.
        vault_root: vault root on disk.
        condition_evaluator: pluggable evaluator for ``state_derived``
            conditions. Defaults to a no-op that treats every condition
            as ``False`` (production wiring lives in ``kernel.proactive``).

    Returns:
        A list of pending reminder rows that are due.
    """
    now = now or datetime.now(tz=timezone.utc)
    events_path = Path(vault_root) / _EVENTS_RELATIVE
    pending = _pending_reminders(events_path)

    evaluate = condition_evaluator or (lambda _c, *, vault_root: False)
    vault_path = Path(vault_root)

    due: list[dict] = []
    for row in pending:
        kind = row.get("kind")
        if kind == "scheduled":
            if _is_scheduled_due(row, now):
                due.append(dict(row))
        elif kind == "state_derived":
            condition = row.get("condition")
            if isinstance(condition, str) and condition.strip():
                try:
                    fired = bool(evaluate(condition, vault_root=vault_path))
                except Exception:  # noqa: BLE001
                    fired = False
                if fired:
                    due.append(dict(row))
    return due
