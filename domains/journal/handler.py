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
from typing import Callable, Optional, Protocol

import yaml

from kernel.claude_runner import ClaudeResponse, invoke as claude_invoke
from kernel.retrieval import ContextBundle
from kernel.session import Session
from kernel.vault import atomic_write

__all__ = ["JournalReadResult", "JournalWriteResult", "read", "write"]


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


# ---------------------------------------------------------------------------
# Read path (issue #3) — narrative-query answering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalReadResult:
    """Return value of ``read`` — what the kernel needs to audit-log + reply.

    Attributes:
        intent: the registered intent label (e.g. ``journal.query``).
        reply_text: the LLM-produced answer string for the user.
        consulted_paths: every file the agent saw in the bundle (audit + citation).
        tokens_in: prompt token count from the runner.
        tokens_out: completion token count from the runner.
    """

    intent: str
    reply_text: str
    consulted_paths: tuple[Path, ...]
    tokens_in: int
    tokens_out: int


class _ClaudeInvoker(Protocol):
    """The subset of ``claude_runner.invoke`` the read path needs."""

    def __call__(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> ClaudeResponse: ...


_READ_PROMPT_PATH = (
    Path(__file__).parent.parent.parent / "kernel" / "prompts" / "read_journal.md"
)


def _load_system_prompt() -> str:
    """Read the journal-read system prompt; fall back to empty if absent.

    The dedicated ``kernel/prompts/read_journal.md`` keeps system.md
    untouched (per CLAUDE.md off-limits list) while still letting the
    read path teach the agent how to use the ``ContextBundle`` and how
    to cite sources.
    """
    try:
        return _READ_PROMPT_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _format_snippet(source: str, text: str) -> str:
    """Render one snippet block with a stable header so the LLM can cite by source."""
    body = text.rstrip()
    return f"## Source: {source}\n\n{body}\n"


def _compose_read_prompt(*, query: str, context_bundle: ContextBundle) -> str:
    """Build the per-turn read prompt: context blocks + question + cite directive."""
    if not context_bundle.snippets:
        context_block = "(no notes were retrieved for this query)"
    else:
        context_block = "\n".join(
            _format_snippet(s.source, s.text) for s in context_bundle.snippets
        )

    paths_block = (
        "\n".join(f"- {p}" for p in context_bundle.paths)
        if context_bundle.paths
        else "(none)"
    )

    return (
        "# Retrieved notes\n\n"
        f"{context_block}\n\n"
        "# Consulted files\n\n"
        f"{paths_block}\n\n"
        "# User question\n\n"
        f"{query}\n\n"
        "# Instructions\n\n"
        "Answer the user's question using only the retrieved notes above. "
        "Cite the file paths you used. If the notes do not contain the answer, "
        "say so explicitly rather than speculating.\n"
    )


def read(
    *,
    intent: str,
    query: str,
    context_bundle: ContextBundle,
    invoker: Optional[_ClaudeInvoker] = None,
) -> JournalReadResult:
    """Answer a journal query using the retrieved context bundle.

    Args:
        intent: registered intent label (must be ``journal.query`` for v1).
        query: the user's free-form question.
        context_bundle: what ``kernel.retrieval.gather_context`` produced.
        invoker: pluggable ``claude_runner.invoke`` for tests; defaults to
            the production runner so production callers don't have to wire it.

    Returns:
        A ``JournalReadResult`` carrying the reply text, the consulted paths
        (for audit + citation), and token telemetry.
    """
    runner = invoker or claude_invoke

    prompt = _compose_read_prompt(query=query, context_bundle=context_bundle)
    system_prompt = _load_system_prompt()

    response = runner(prompt, system_prompt=system_prompt or None)

    return JournalReadResult(
        intent=intent,
        reply_text=response.text,
        consulted_paths=context_bundle.paths,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
    )
