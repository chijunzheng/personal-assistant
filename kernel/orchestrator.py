"""Per-turn dispatch + single-instance enforcement.

The orchestrator owns three responsibilities for issue #1's tracer slice:

  1. Hold a process-exclusive ``flock`` on the configured lock path so a
     second bot instance refuses to start (``kernel/RUNTIME.md`` ->
     "Single-instance enforcement"; ``kernel/SYNC.md`` defense #5 reasoning).
  2. For each incoming message, invoke ``claude_runner.invoke`` with a
     generic system prompt and return the LLM's reply.
  3. Append one audit-log entry per turn capturing token telemetry,
     duration, and outcome.

Classification, retrieval, and plugin dispatch live behind this seam in
later issues — the contract here is intentionally narrow so the tracer
slice stays small and the kernel's later expansion does not require
reworking the per-turn flow.
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

__all__ = [
    "DEFAULT_LOCK_PATH",
    "InstanceLockError",
    "Orchestrator",
    "OrchestratorReply",
    "SingleInstanceLock",
]


DEFAULT_LOCK_PATH = Path("/tmp/personal-assistant.lock")
DEFAULT_AUDIT_ROOT = Path("vault/_audit")
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
    """Wires per-turn flow: claude invoke -> audit-log -> reply.

    Dependencies are injected (``invoker``, ``audit_writer``, ``clock``)
    so tests don't have to monkeypatch internals. Production callers can
    rely on the defaults.
    """

    def __init__(
        self,
        *,
        lock: Optional[SingleInstanceLock] = None,
        audit_root: str | os.PathLike[str] = DEFAULT_AUDIT_ROOT,
        invoker: Optional[_ClaudeInvoker] = None,
        audit_writer: Optional[Callable[..., object]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        config_label: str = "default",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self._lock = lock or SingleInstanceLock(DEFAULT_LOCK_PATH)
        self._audit_root = Path(audit_root)
        self._invoker = invoker or claude_invoke
        self._audit_writer = audit_writer or write_audit_entry
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self._config_label = config_label
        self._system_prompt = system_prompt

    def start(self) -> None:
        """Acquire the single-instance lock — call once at process start."""
        self._lock.acquire()

    def stop(self) -> None:
        """Release the single-instance lock — call once at process shutdown."""
        self._lock.release()

    def handle_message(self, message: str) -> OrchestratorReply:
        """Run one tracer turn: invoke claude_runner, log, return the reply."""
        started_ts = self._clock()
        wall_start = time.monotonic()
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
