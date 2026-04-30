"""Per-turn dispatch + single-instance enforcement.

The orchestrator owns:

  1. Hold a process-exclusive ``flock`` on the configured lock path so a
     second bot instance refuses to start (``kernel/RUNTIME.md`` ->
     "Single-instance enforcement"; ``kernel/SYNC.md`` defense #5 reasoning).
  2. For each incoming message:
     a. Classify (kernel.classifier) -> intent label
     b. Dispatch: if ``journal.*``, call the journal handler write path
        and audit-log the write; otherwise fall through to the generic
        ``claude_runner.invoke`` echo path (issue #1's tracer behavior is
        the fallback for unrecognized intents).
     c. Update the active session.
  3. Append audit-log entries per operation capturing token telemetry,
     duration, and outcome.

Retrieval and per-domain query dispatch (read paths for journal, finance,
inventory, fitness, reminder) live behind this seam in later issues — the
contract here grows by adding new ``intent.startswith(...)`` dispatch
branches *only when a new write path needs the orchestrator to wire it*.
Read/query paths route through ``kernel.retrieval`` (issue #10).
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

from kernel.audit import write_audit_entry
from kernel.claude_runner import ClaudeResponse, ClaudeRunnerError, invoke as claude_invoke
from kernel.classifier import Classifier, FALLBACK_INTENT
from kernel.retrieval import gather_context
from kernel.session import Session, load_or_create as session_load, update as session_update

__all__ = [
    "DEFAULT_LOCK_PATH",
    "InstanceLockError",
    "Orchestrator",
    "OrchestratorReply",
    "SingleInstanceLock",
]


DEFAULT_LOCK_PATH = Path("/tmp/personal-assistant.lock")
DEFAULT_AUDIT_ROOT = Path("vault/_audit")
DEFAULT_VAULT_ROOT = Path("vault")
DEFAULT_CHAT_ID = "default"
DEFAULT_SYSTEM_PROMPT = (
    "You are a concise personal-assistant tracer. Reply briefly and helpfully."
)


class InstanceLockError(RuntimeError):
    """Raised when the kernel's single-instance lock cannot be acquired."""


class _ClaudeInvoker(Protocol):
    """The subset of the ``claude_runner`` API the orchestrator needs."""

    def __call__(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> ClaudeResponse: ...


class SingleInstanceLock:
    """Process-exclusive ``flock`` wrapper.

    Used by the orchestrator at startup; second instances raise
    ``InstanceLockError`` rather than silently double-running and racing
    the audit log.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        """Open the lock file and ``flock`` it non-blockingly."""
        if self._fd is not None:
            return  # already held by this object
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            os.close(fd)
            if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise InstanceLockError(
                    f"another instance already holds {self._path}"
                ) from err
            raise
        self._fd = fd

    def release(self) -> None:
        """Release the lock and close the underlying fd."""
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


@dataclass(frozen=True)
class OrchestratorReply:
    """Return value of ``Orchestrator.handle_message``.

    ``text`` is what gets sent back over Telegram; the other fields are
    surfaced for the bridge / tests to assert against.
    """

    text: str
    tokens_in: int
    tokens_out: int
    duration_ms: int


class Orchestrator:
    """Wires per-turn flow: classify -> dispatch -> audit-log -> reply.

    Dependencies are injected (``invoker``, ``classifier``, ``audit_writer``,
    ``clock``) so tests don't have to monkeypatch internals. Production
    callers can rely on the defaults.

    The dispatch table is intentionally tiny right now: ``journal.*`` ->
    journal handler write; everything else falls through to the generic
    echo path (issue #1's behavior is preserved as the fallback for
    unrecognized intents per issue #2's spec).
    """

    def __init__(
        self,
        *,
        lock: Optional[SingleInstanceLock] = None,
        audit_root: str | os.PathLike[str] = DEFAULT_AUDIT_ROOT,
        vault_root: str | os.PathLike[str] = DEFAULT_VAULT_ROOT,
        invoker: Optional[_ClaudeInvoker] = None,
        audit_writer: Optional[Callable[..., object]] = None,
        classifier: Optional[Classifier] = None,
        clock: Optional[Callable[[], datetime]] = None,
        config_label: str = "default",
        config: Optional[dict] = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        chat_id: str = DEFAULT_CHAT_ID,
    ) -> None:
        self._lock = lock or SingleInstanceLock(DEFAULT_LOCK_PATH)
        self._audit_root = Path(audit_root)
        self._vault_root = Path(vault_root)
        self._invoker = invoker or claude_invoke
        self._audit_writer = audit_writer or write_audit_entry
        self._classifier = classifier
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self._config_label = config_label
        self._config = config or {}
        self._system_prompt = system_prompt
        self._chat_id = chat_id

    def start(self) -> None:
        """Acquire the single-instance lock — call once at process start."""
        self._lock.acquire()

    def stop(self) -> None:
        """Release the single-instance lock — call once at process shutdown."""
        self._lock.release()

    def handle_message(self, message: str) -> OrchestratorReply:
        """Run one turn: classify, dispatch, audit, reply."""
        started_ts = self._clock()
        wall_start = time.monotonic()

        # Step 1: classify (if a classifier is wired). With no classifier we
        # preserve the issue-#1 echo path verbatim.
        intent = self._classify(message, started_ts)

        # Step 2: dispatch.
        # journal.query -> journal read (retrieval + LLM)
        # journal.*     -> journal write (capture path)
        # everything else -> generic echo (issue #1 fallback)
        if intent == "journal.query":
            return self._handle_journal_query(
                intent=intent,
                message=message,
                started_ts=started_ts,
                wall_start=wall_start,
            )
        if intent.startswith("journal."):
            return self._handle_journal(
                intent=intent,
                message=message,
                started_ts=started_ts,
                wall_start=wall_start,
            )

        return self._handle_echo(
            message=message,
            started_ts=started_ts,
            wall_start=wall_start,
        )

    # -- private ---------------------------------------------------------

    def _classify(self, message: str, started_ts: datetime) -> str:
        """Run the classifier and audit-log the result. Returns the intent."""
        if self._classifier is None:
            return ""  # echo path; no classification performed

        wall_start = time.monotonic()
        outcome = "ok"
        error_message: Optional[str] = None
        intent = FALLBACK_INTENT
        try:
            intent = self._classifier.classify(message)
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "classify",
            "actor": "kernel.orchestrator",
            "outcome": outcome,
            "duration_ms": int((time.monotonic() - wall_start) * 1000),
            "config": self._config_label,
            "intent": intent,
        }
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)
        return intent

    def _handle_journal(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch a ``journal.*`` intent to the journal plugin's write path."""
        # Imported lazily to keep the kernel free of compile-time plugin imports
        # (the plugin contract is "kernel discovers, never knows by name").
        from domains.journal.handler import write as journal_write

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        outcome = "ok"
        error_message: Optional[str] = None
        path: Optional[Path] = None
        content_sha: Optional[str] = None
        try:
            result = journal_write(
                intent=intent,
                message=message,
                session=session,
                vault_root=self._vault_root,
                clock=self._clock,
            )
            path = result.path
            content_sha = result.content_sha256
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "write",
            "actor": "kernel.orchestrator",
            "domain": "journal",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
        }
        if path is not None:
            entry["path"] = str(path)
        if content_sha is not None:
            entry["sha256_after"] = content_sha
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error" or path is None:
            return OrchestratorReply(
                text="Sorry — something went wrong saving that to the journal.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        # Update the active session with a brief note about what just happened.
        session_update(
            session,
            f"journal capture -> {path.name}",
            vault_root=self._vault_root,
            clock=self._clock,
        )

        # Reply with a path the user (or a future sub-agent) can verify.
        try:
            display_path = path.relative_to(self._vault_root)
        except ValueError:
            display_path = path
        return OrchestratorReply(
            text=f"Saved to journal/{Path(display_path).name}.",
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
        )

    def _handle_journal_query(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch a ``journal.query`` intent through retrieval -> read -> reply.

        Wires:
          1. ``kernel.retrieval.gather_context`` -> ContextBundle
          2. ``domains.journal.handler.read``    -> reply text + paths
          3. Audit-log a ``read`` op carrying every consulted path
        """
        # Plugin imports stay lazy — kernel knows the plugin only by registry.
        from domains.journal.handler import read as journal_read

        bundle = gather_context(
            query=message,
            config=self._config,
            vault_root=self._vault_root,
            domain="journal",
        )

        outcome = "ok"
        error_message: Optional[str] = None
        reply_text = ""
        tokens_in = 0
        tokens_out = 0
        paths: tuple = bundle.paths
        try:
            result = journal_read(
                intent=intent,
                query=message,
                context_bundle=bundle,
                invoker=self._invoker,
            )
            reply_text = result.reply_text
            tokens_in = result.tokens_in
            tokens_out = result.tokens_out
            paths = result.consulted_paths
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "read",
            "actor": "kernel.orchestrator",
            "domain": "journal",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "paths": [str(p) for p in paths],
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error":
            return OrchestratorReply(
                text="Sorry — something went wrong reading from the journal.",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                duration_ms=duration_ms,
            )

        return OrchestratorReply(
            text=reply_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=duration_ms,
        )

    def _handle_echo(
        self,
        *,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Issue-#1 fallback: invoke the runner, log, return its text."""
        outcome = "ok"
        error_message: Optional[str] = None
        response: Optional[ClaudeResponse] = None

        try:
            response = self._invoker(message, system_prompt=self._system_prompt)
        except ClaudeRunnerError as err:
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "echo",
            "actor": "kernel.orchestrator",
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
        }
        if response is not None:
            entry["tokens_in"] = response.tokens_in
            entry["tokens_out"] = response.tokens_out
        if error_message is not None:
            entry["error"] = error_message

        self._audit_writer(entry, audit_root=self._audit_root)

        if response is None:
            return OrchestratorReply(
                text="Sorry — something went wrong handling that message.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        return OrchestratorReply(
            text=response.text,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            duration_ms=duration_ms,
        )
