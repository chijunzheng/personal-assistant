"""Journal plugin write path — narrative markdown notes with frontmatter.

A journal capture is one Telegram message persisted to
``vault/journal/{date}-{slug}.md`` as a markdown file with this
frontmatter:

    ---
    date: <ISO8601 of the capture>
    tags: [<LLM-extracted topical tags>]
    links: []                       # populated by a later pass (issue #4/#10)
    source: telegram
    session_id: <session uuid>
    ---

    <user's verbatim message>

The single load-bearing invariant is **idempotency on content sha256**.
The same intent + message + session_id pair always derives the same
filename and the same file body, so Drive sync replays and classifier
retries cannot duplicate notes. The slug includes a short content-hash
suffix so two different messages on the same day cannot collide onto the
same path.

The handler is plugin code; it must not write the audit log itself —
the kernel does that after ``write`` returns (per CLAUDE.md "plugins
must be log-silent").
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from kernel.session import Session
from kernel.vault import atomic_write

__all__ = ["JournalWriteResult", "write"]


_MAX_SLUG_WORDS = 6
_HASH_PREFIX_LEN = 8


@dataclass(frozen=True)
class JournalWriteResult:
    """Return value of ``write`` — what the kernel needs to audit-log + reply."""

    intent: str
    path: Path
    content_sha256: str
    tags: tuple[str, ...]


def _now(clock: Optional[Callable[[], datetime]] = None) -> datetime:
    return (clock or (lambda: datetime.now(tz=timezone.utc)))()


def _slugify_message(message: str) -> str:
    """Build a kebab-case slug from the first ``_MAX_SLUG_WORDS`` informative words."""
    cleaned = re.sub(r"[^a-z0-9\s]", "", message.lower())
    words = [w for w in cleaned.split() if w]
    if not words:
        return "note"
    return "-".join(words[:_MAX_SLUG_WORDS])


def _content_hash(intent: str, message: str, session_id: str) -> str:
    """Stable sha256 over the inputs that uniquely identify a capture."""
    serialized = f"{intent}\n{session_id}\n{message}"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _render(
    *,
    message: str,
    timestamp: str,
    tags: list[str],
    session_id: str,
) -> str:
    """Render the markdown file (frontmatter + body)."""
    front = {
        "date": timestamp,
        "tags": tags,
        "links": [],
        "source": "telegram",
        "session_id": session_id,
    }
    head = yaml.safe_dump(front, sort_keys=True).strip()
    return (
        "---\n"
        f"{head}\n"
        "---\n\n"
        f"{message.strip()}\n"
    )


def write(
    *,
    intent: str,
    message: str,
    session: Session,
    vault_root: str | os.PathLike[str],
    clock: Optional[Callable[[], datetime]] = None,
    tag_extractor: Optional[Callable[[str], list[str]]] = None,
) -> JournalWriteResult:
    """Persist a journal capture and return the resulting file metadata.

    Args:
        intent: registered intent label (must be ``journal.capture`` for v1).
        message: the user's verbatim text to persist.
        session: active session — supplies ``session_id`` for frontmatter.
        vault_root: root of the vault on disk.
        clock: pluggable wall clock for tests.
        tag_extractor: pluggable LLM tag extractor — defaults to "no tags"
            so the v1 path doesn't require the runner. The orchestrator
            wires the real extractor in production.

    Idempotency: identical ``(intent, message, session.session_id)`` tuples
    derive identical paths and identical content; re-running is a safe no-op.
    """
    if not message or not message.strip():
        raise ValueError("journal write requires a non-empty message")

    extract = tag_extractor or (lambda _msg: [])
    raw_tags = extract(message) or []
    tags = [str(t) for t in raw_tags]

    now = _now(clock)
    iso_ts = now.isoformat()
    date_prefix = now.date().isoformat()  # YYYY-MM-DD

    digest = _content_hash(intent, message, session.session_id)
    slug = f"{_slugify_message(message)}-{digest[:_HASH_PREFIX_LEN]}"
    path = Path(vault_root) / "journal" / f"{date_prefix}-{slug}.md"

    rendered = _render(
        message=message,
        timestamp=iso_ts,
        tags=tags,
        session_id=session.session_id,
    )

    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        # Idempotent no-op: the byte-identical file is already on disk.
        return JournalWriteResult(
            intent=intent,
            path=path,
            content_sha256=digest,
            tags=tuple(tags),
        )

    atomic_write(path, rendered)
    return JournalWriteResult(
        intent=intent,
        path=path,
        content_sha256=digest,
        tags=tuple(tags),
    )
