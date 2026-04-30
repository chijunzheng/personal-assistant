"""Active session manager.

The session is a single markdown file at
``vault/_index/active_session.md`` carrying a YAML frontmatter (chat id,
session id, started_at, last_updated, turn count) and a running summary
body. It is read at the top of every turn (engineering decision #4
``active_session_summary``) and updated after dispatch completes.

For v1 we keep one active session per vault. When a new ``chat_id`` arrives,
the previous session is replaced — Telegram is single-user/single-chat and
the operating model assumes one logical conversation at a time.

Concurrency: writes go through ``kernel.vault.atomic_write`` so a Drive
sync mid-update never observes a half-written file (SYNC.md defense #1).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from kernel.vault import atomic_write

__all__ = ["Session", "load_or_create", "update"]


_SESSION_RELATIVE_PATH = Path("_index") / "active_session.md"
_FRONTMATTER_DELIMITER = "---"


@dataclass(frozen=True)
class Session:
    """Immutable view of the active session."""

    chat_id: str
    session_id: str
    started_at: str  # ISO8601
    last_updated: str  # ISO8601
    turns: int
    summary: str  # markdown body, may contain newlines


def _session_path(vault_root: str | os.PathLike[str]) -> Path:
    return Path(vault_root) / _SESSION_RELATIVE_PATH


def _now_iso(clock: Optional[callable] = None) -> str:
    fn = clock or (lambda: datetime.now(tz=timezone.utc))
    return fn().isoformat()


def _serialize(session: Session) -> str:
    """Render a Session as markdown frontmatter + summary body."""
    frontmatter = {
        "chat_id": session.chat_id,
        "session_id": session.session_id,
        "started_at": session.started_at,
        "last_updated": session.last_updated,
        "turns": session.turns,
    }
    head = yaml.safe_dump(frontmatter, sort_keys=True).strip()
    body = session.summary.rstrip()
    return (
        f"{_FRONTMATTER_DELIMITER}\n"
        f"{head}\n"
        f"{_FRONTMATTER_DELIMITER}\n\n"
        f"{body}\n"
    )


def _deserialize(raw: str) -> Optional[Session]:
    """Parse the on-disk session file back into a Session, or None if malformed."""
    if not raw.startswith(_FRONTMATTER_DELIMITER):
        return None
    parts = raw.split(_FRONTMATTER_DELIMITER, 2)
    if len(parts) < 3:
        return None
    head = parts[1].strip()
    body = parts[2].lstrip("\n").rstrip()
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    required = ("chat_id", "session_id", "started_at", "last_updated", "turns")
    if not all(field in meta for field in required):
        return None
    return Session(
        chat_id=str(meta["chat_id"]),
        session_id=str(meta["session_id"]),
        started_at=str(meta["started_at"]),
        last_updated=str(meta["last_updated"]),
        turns=int(meta["turns"]),
        summary=body,
    )


def load_or_create(
    chat_id: str,
    *,
    vault_root: str | os.PathLike[str],
    clock: Optional[callable] = None,
) -> Session:
    """Return the active session for ``chat_id``, creating it if absent.

    If the on-disk session belongs to a different ``chat_id`` (or the file is
    missing/malformed), a fresh session is materialized and written.
    """
    path = _session_path(vault_root)
    if path.exists():
        existing = _deserialize(path.read_text(encoding="utf-8"))
        if existing is not None and existing.chat_id == chat_id:
            return existing

    now = _now_iso(clock)
    session = Session(
        chat_id=chat_id,
        session_id=uuid.uuid4().hex,
        started_at=now,
        last_updated=now,
        turns=0,
        summary="",
    )
    atomic_write(path, _serialize(session))
    return session


def update(
    session: Session,
    note: str,
    *,
    vault_root: str | os.PathLike[str],
    clock: Optional[callable] = None,
) -> Session:
    """Append a one-line note to the running summary and bump the turn count.

    Returns the new (immutable) Session and persists it to disk.
    """
    body = session.summary.rstrip()
    appended = f"{body}\n- {note.strip()}".strip()
    next_session = replace(
        session,
        last_updated=_now_iso(clock),
        turns=session.turns + 1,
        summary=appended,
    )
    atomic_write(_session_path(vault_root), _serialize(next_session))
    return next_session
