"""Append-only verbatim chat log.

The chat log carries the *verbatim* last-N user/bot turns so the
orchestrator can resolve referential follow-ups like *"yes, walk me
through it"* against the literal antecedent. The compact
``active_session.md`` summary intentionally loses that detail; this log
preserves it.

Layout matches the ``_audit/`` convention so the kernel only has one
mental model for "append-only, daily-rotated, content-hash-deduped JSONL
under an underscore-prefixed kernel directory":

    <vault_root>/_chat_log/<chat_id>/<YYYY-MM-DD>.jsonl

Per-``chat_id`` subdirectory keeps multi-chat vaults cleanly separable
without per-line filtering on the read path's hot loop.

Concurrency invariants from ``kernel/SYNC.md``:

  * **Defense 1 — atomic writes.** Every append goes through
    ``kernel.vault.atomic_write`` (read-modify-write because JSONL append
    cannot be atomic on its own without ``O_APPEND`` race tradeoffs that
    iCloud's sync layer doesn't honor).
  * **Defense 5 — append-only with sha256 IDs.** Readers dedupe by
    ``id`` so a duplicate append (same content) produces only one
    logical turn on load.

The module deliberately exposes a small surface (one frozen dataclass,
``load_recent``, ``append``) per the deep-narrow API contract in the
ADR at ``docs/decisions/2026-05-10-chat-context-and-capabilities.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from kernel.vault import atomic_write

__all__ = ["ChatTurn", "append", "load_recent"]


_CHAT_LOG_RELATIVE = Path("_chat_log")

# Hard cap on the backward walk through daily files when ``load_recent``
# can't satisfy ``n`` from today's file alone. A long-idle chat has no
# bound on how far back the last turn might be, but in practice nothing
# the orchestrator does with a 5-turn window cares about turns from
# weeks ago — and we don't want to scan the entire vault history per
# call. 90 days is generous and bounded.
_MAX_DAYS_BACK = 90


@dataclass(frozen=True)
class ChatTurn:
    """One user message + the bot's reply, with the routed intent.

    ``intent`` may be the empty string for the echo / fallback path
    (issue #25 only wires the echo handler; structured handlers will
    populate it under #27). ``id`` is the sha256 over the canonicalized
    payload — readers dedupe by it.
    """

    id: str
    ts: str  # ISO8601, UTC
    chat_id: str
    user_msg: str
    bot_reply: str
    intent: str


def _chat_dir(vault_root: str | os.PathLike[str], chat_id: str) -> Path:
    """Per-``chat_id`` directory under ``<vault_root>/_chat_log/``."""
    return Path(vault_root) / _CHAT_LOG_RELATIVE / chat_id


def _daily_path(vault_root: str | os.PathLike[str], chat_id: str, day: date) -> Path:
    """Resolve the dated JSONL file for ``(chat_id, day)``."""
    return _chat_dir(vault_root, chat_id) / f"{day.isoformat()}.jsonl"


def _now_utc(clock: Optional[Callable[[], datetime]] = None) -> datetime:
    """Resolve the current UTC datetime, honoring an injected clock."""
    fn = clock or (lambda: datetime.now(tz=timezone.utc))
    return fn()


def _turn_id(*, chat_id: str, ts: str, user_msg: str, bot_reply: str) -> str:
    """Stable sha256 over the content fields per the ADR.

    Using the four fields the ADR specifies (``chat_id|ts|user_msg|bot_reply``)
    means two appends with the same wall-clock timestamp and identical
    text content collapse to one logical turn — which is the point of
    idempotency on retry.
    """
    canonical = f"{chat_id}|{ts}|{user_msg}|{bot_reply}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_turns(path: Path) -> list[ChatTurn]:
    """Parse one JSONL daily file into ChatTurns, deduped by id.

    Order is preserved (earliest line first). A malformed line is
    skipped rather than blowing up the whole read — the chat log is a
    convenience signal for prompt assembly, not a correctness-critical
    artifact, and we'd rather degrade gracefully than refuse to answer.
    """
    if not path.exists():
        return []
    seen: set[str] = set()
    turns: list[ChatTurn] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            turn_id = record.get("id")
            if not turn_id or turn_id in seen:
                continue
            try:
                turn = ChatTurn(
                    id=str(turn_id),
                    ts=str(record["ts"]),
                    chat_id=str(record["chat_id"]),
                    user_msg=str(record["user_msg"]),
                    bot_reply=str(record["bot_reply"]),
                    intent=str(record.get("intent", "")),
                )
            except KeyError:
                continue
            seen.add(turn_id)
            turns.append(turn)
    return turns


def load_recent(
    chat_id: str,
    n: int,
    vault_root: str | os.PathLike[str],
) -> list[ChatTurn]:
    """Return the most-recent ``n`` turns for ``chat_id``, chronological.

    Walks daily files backward from today until ``n`` turns have been
    collected or ``_MAX_DAYS_BACK`` days have been scanned (whichever
    comes first). Within a day, on-disk order is preserved (append
    order). Across days, older days come first in the returned list —
    callers can therefore embed the result directly into a prompt with
    *"newest at the bottom"* semantics.

    Returns an empty list when ``n <= 0`` or the chat has no log yet.
    """
    if n <= 0:
        return []

    chat_dir = _chat_dir(vault_root, chat_id)
    if not chat_dir.exists():
        return []

    today = _now_utc().date()
    collected: list[ChatTurn] = []
    for offset in range(_MAX_DAYS_BACK):
        day = today - timedelta(days=offset)
        day_turns = _read_turns(_daily_path(vault_root, chat_id, day))
        if day_turns:
            # Prepend so the final list stays chronological.
            collected = day_turns + collected
            if len(collected) >= n:
                break

    return collected[-n:]


def append(
    chat_id: str,
    *,
    user_msg: str,
    bot_reply: str,
    intent: str,
    vault_root: str | os.PathLike[str],
    clock: Optional[Callable[[], datetime]] = None,
) -> ChatTurn:
    """Append one ``ChatTurn`` to the dated JSONL file, idempotently.

    The write is read-modify-write through ``kernel.vault.atomic_write``
    so a sync mid-write never observes a half-written file. Appending
    the same ``(chat_id, ts, user_msg, bot_reply)`` twice produces only
    one logical entry on load (sha256 dedup).
    """
    now = _now_utc(clock)
    ts = now.isoformat()
    turn = ChatTurn(
        id=_turn_id(chat_id=chat_id, ts=ts, user_msg=user_msg, bot_reply=bot_reply),
        ts=ts,
        chat_id=chat_id,
        user_msg=user_msg,
        bot_reply=bot_reply,
        intent=intent,
    )

    target = _daily_path(vault_root, chat_id, now.date())
    target.parent.mkdir(parents=True, exist_ok=True)

    # Read existing lines (if any), append the new one, write atomically.
    # JSONL append on its own can't be atomic against a syncing reader
    # observing a partial trailing line, so we route through
    # ``atomic_write`` (tmp + os.replace) at the cost of one extra read
    # per append. The chat log is small (5 turns/day typical) so this
    # tradeoff is negligible and buys us SYNC.md defense #1.
    existing = ""
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    # Idempotency: if the same turn id already exists in the file,
    # short-circuit and return the existing turn shape rather than
    # writing a duplicate line.
    if existing and turn.id in existing:
        # Defensive parse — only treat the id as a real hit if it shows
        # up as a JSON record's ``id`` field, not as part of message
        # content.
        for line in existing.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("id") == turn.id:
                return turn

    serialized = json.dumps(
        {
            "id": turn.id,
            "ts": turn.ts,
            "chat_id": turn.chat_id,
            "user_msg": turn.user_msg,
            "bot_reply": turn.bot_reply,
            "intent": turn.intent,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )

    if existing and not existing.endswith("\n"):
        existing = existing + "\n"
    new_content = existing + serialized + "\n"

    atomic_write(target, new_content)
    return turn
