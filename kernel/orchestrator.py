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
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

from kernel.audit import write_audit_entry
from kernel.claude_runner import ClaudeResponse, ClaudeRunnerError, invoke as claude_invoke
from kernel.classifier import Classifier, FALLBACK_INTENT
from kernel.index import refresh as index_refresh_default
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

# Where the writes-since-last-refresh counter is persisted. Lives under
# ``vault/_index/`` next to INDEX.md so it shares the same Drive sync scope.
_REFRESH_STATE_RELATIVE = Path("_index") / ".refresh_state.json"

# Default threshold pulled from configs/default.yaml; the orchestrator
# accepts an override via ``config.context_engineering.index_refresh_after_writes``.
_DEFAULT_INDEX_REFRESH_AFTER_WRITES = 5


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
        index_refresh: Optional[Callable[..., object]] = None,
        finance_extractor: Optional[Callable[[str], list]] = None,
        finance_query_parser: Optional[Callable[[str], dict]] = None,
        inventory_extractor: Optional[Callable[[str, str], dict]] = None,
        inventory_query_parser: Optional[Callable[[str], dict]] = None,
        fitness_extractor: Optional[Callable[[str, str], dict]] = None,
        fitness_query_parser: Optional[Callable[[str], dict]] = None,
        reminder_extractor: Optional[Callable[[str, str], dict]] = None,
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
        # Pluggable index refresh entry point (default: kernel.index.refresh).
        # Tests inject a spy that wraps the real refresh; production uses the
        # default so callers don't have to wire it explicitly.
        self._index_refresh = index_refresh or index_refresh_default
        # Pluggable finance hooks — tests inject deterministic stand-ins so
        # neither the LLM-backed extractor nor the LLM-backed query parser
        # actually runs during integration tests. Production callers leave
        # these as ``None`` and the handler shells to ``claude_runner``.
        self._finance_extractor = finance_extractor
        self._finance_query_parser = finance_query_parser
        # Pluggable inventory hooks — same pattern as finance: tests inject
        # deterministic stand-ins so neither the LLM-backed extractor nor the
        # LLM-backed query parser actually runs during integration tests.
        self._inventory_extractor = inventory_extractor
        self._inventory_query_parser = inventory_query_parser
        # Pluggable fitness hooks — same pattern again. The extractor is
        # called with ``(message, intent)`` because the same plugin handles
        # four logging intents with very different output shapes.
        self._fitness_extractor = fitness_extractor
        self._fitness_query_parser = fitness_query_parser
        # Pluggable reminder hook — the extractor is called with
        # ``(message, intent)`` because the same plugin handles add /
        # add_when / cancel with different output shapes.
        self._reminder_extractor = reminder_extractor

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
        # journal.query        -> journal read (retrieval + LLM)
        # journal.*            -> journal write (capture path)
        # finance.query        -> finance read (structured query_finance)
        # finance.transaction  -> finance write (extract + append)
        # inventory.{add,consume,adjust} -> inventory write (event log + state)
        # inventory.{query,list_low}     -> inventory read (query_inventory)
        # fitness.{workout_log,meal_log,metric_log,profile_update}
        #                      -> fitness write (logging surface, issue #7)
        # fitness.query        -> fitness read (query_fitness)
        # everything else      -> generic echo (issue #1 fallback)
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
        if intent == "finance.query":
            return self._handle_finance_query(
                intent=intent,
                message=message,
                started_ts=started_ts,
                wall_start=wall_start,
            )
        if intent == "finance.transaction":
            return self._handle_finance_transaction(
                intent=intent,
                message=message,
                started_ts=started_ts,
                wall_start=wall_start,
            )
        if intent in ("inventory.add", "inventory.consume", "inventory.adjust"):
            return self._handle_inventory_write(
                intent=intent,
                message=message,
                started_ts=started_ts,
                wall_start=wall_start,
            )
        if intent in ("inventory.query", "inventory.list_low"):
            return self._handle_inventory_read(
                intent=intent,
                message=message,
                started_ts=started_ts,
                wall_start=wall_start,
            )
        if intent in (
            "fitness.workout_log",
            "fitness.meal_log",
            "fitness.metric_log",
            "fitness.profile_update",
        ):
            return self._handle_fitness_write(
                intent=intent,
                message=message,
                started_ts=started_ts,
                wall_start=wall_start,
            )
        if intent == "fitness.query":
            return self._handle_fitness_query(
                intent=intent,
                message=message,
                started_ts=started_ts,
                wall_start=wall_start,
            )
        if intent in ("fitness.workout_plan", "fitness.nutrition_plan"):
            return self._handle_fitness_plan(
                intent=intent,
                message=message,
                started_ts=started_ts,
                wall_start=wall_start,
            )
        if intent in ("reminder.add", "reminder.add_when", "reminder.cancel"):
            return self._handle_reminder_write(
                intent=intent,
                message=message,
                started_ts=started_ts,
                wall_start=wall_start,
            )
        if intent == "reminder.list":
            return self._handle_reminder_list(
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

        # The journal write succeeded -> bump the writes-since-last-refresh
        # counter. When the counter reaches the configured threshold, the
        # helper calls ``index.refresh()`` inline, audit-logs the refresh,
        # and resets the counter. (Issue #4 acceptance criterion.)
        self._maybe_refresh_index(started_ts=started_ts)

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
            # Issue #10: bundle telemetry — these are what the eval harness
            # aggregates per turn to chart engineered-vs-baseline divergence.
            "tokens_in_context_bundle": int(getattr(bundle, "tokens_estimate", 0)),
            "flags": dict(getattr(bundle, "flags", {})),
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

    def _handle_finance_transaction(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch ``finance.transaction`` to the finance plugin's write path.

        Wires:
          1. ``domains.finance.handler.write`` -> appends transactions.jsonl
          2. Audit-log a ``write`` op with ``domain=finance`` and the path
          3. Trigger the every-5-writes index refresh
          4. Reply with a count + skipped summary so the user can verify
        """
        # Lazy import keeps the kernel decoupled from plugin compile-time.
        from domains.finance.handler import write as finance_write

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        outcome = "ok"
        error_message: Optional[str] = None
        path: Optional[Path] = None
        appended = 0
        skipped = 0
        try:
            result = finance_write(
                intent=intent,
                message=message,
                session=session,
                vault_root=self._vault_root,
                clock=self._clock,
                extractor=self._finance_extractor,
                invoker=self._invoker,
            )
            path = result.path
            appended = result.appended
            skipped = result.skipped
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "write",
            "actor": "kernel.orchestrator",
            "domain": "finance",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
            "appended": appended,
            "skipped": skipped,
        }
        if path is not None:
            entry["path"] = str(path)
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error":
            return OrchestratorReply(
                text="Sorry — something went wrong saving those transactions.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        # Update the active session with a one-line note about the upload.
        session_update(
            session,
            f"finance: appended {appended} transaction(s), skipped {skipped} duplicate(s)",
            vault_root=self._vault_root,
            clock=self._clock,
        )

        # Bump the writes-since-refresh counter just like journal does — only
        # when at least one row genuinely landed on disk.
        if appended > 0:
            self._maybe_refresh_index(started_ts=started_ts)

        if appended == 0 and skipped > 0:
            text = (
                f"Statement already on file — {skipped} transaction(s) were "
                f"already recorded; 0 new rows appended."
            )
        else:
            text = (
                f"Recorded {appended} transaction(s)"
                + (f"; {skipped} already on file" if skipped else "")
                + "."
            )
        return OrchestratorReply(
            text=text,
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
        )

    def _handle_finance_query(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch ``finance.query`` to the finance plugin's read path.

        The finance read does its own structured aggregation (no retrieval
        bundle is needed for a numeric answer). The audit entry records
        the path of the canonical transactions log so the user can
        reconstruct the answer offline.
        """
        from domains.finance.handler import read as finance_read

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        outcome = "ok"
        error_message: Optional[str] = None
        reply_text = ""
        category: Optional[str] = None
        agg: Optional[str] = None
        count = 0
        try:
            result = finance_read(
                intent=intent,
                query=message,
                vault_root=self._vault_root,
                query_parser=self._finance_query_parser,
                invoker=self._invoker,
            )
            reply_text = result.reply_text
            category = result.category
            agg = result.agg
            count = result.count
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "read",
            "actor": "kernel.orchestrator",
            "domain": "finance",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
            "match_count": count,
        }
        if category is not None:
            entry["category"] = category
        if agg is not None:
            entry["agg"] = agg
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error":
            return OrchestratorReply(
                text="Sorry — something went wrong querying your transactions.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        return OrchestratorReply(
            text=reply_text,
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
        )

    def _handle_inventory_write(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch ``inventory.{add,consume,adjust}`` to the inventory write path.

        Wires:
          1. ``domains.inventory.handler.write`` -> appends events.jsonl +
             recomputes state.yaml
          2. Audit-log a ``write`` op with ``domain=inventory`` and the
             events JSONL path
          3. Trigger the every-5-writes index refresh on a successful append
          4. Reply with a brief confirmation the user can verify against state
        """
        # Lazy import keeps the kernel decoupled from plugin compile-time.
        from domains.inventory.handler import write as inventory_write

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        outcome = "ok"
        error_message: Optional[str] = None
        path: Optional[Path] = None
        appended = False
        item: Optional[str] = None
        delta: float = 0.0
        try:
            result = inventory_write(
                intent=intent,
                message=message,
                session=session,
                vault_root=self._vault_root,
                clock=self._clock,
                extractor=self._inventory_extractor,
                invoker=self._invoker,
            )
            path = result.path
            appended = result.appended
            item = result.item
            delta = result.quantity_delta
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "write",
            "actor": "kernel.orchestrator",
            "domain": "inventory",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
            "appended": 1 if appended else 0,
            "skipped": 0 if appended else 1,
        }
        if path is not None:
            entry["path"] = str(path)
        if item is not None:
            entry["item"] = item
            entry["quantity_delta"] = delta
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error":
            return OrchestratorReply(
                text="Sorry — something went wrong updating inventory.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        # One-line note about the inventory change for the running session.
        action = {
            "inventory.add": "added",
            "inventory.consume": "consumed",
            "inventory.adjust": "adjusted",
        }.get(intent, intent)
        session_update(
            session,
            f"inventory: {action} {item} (delta={delta})",
            vault_root=self._vault_root,
            clock=self._clock,
        )

        # Bump the writes-since-refresh counter only when a new event landed.
        if appended:
            self._maybe_refresh_index(started_ts=started_ts)

        if not appended:
            text = (
                f"Already on file — inventory unchanged ({item})."
            )
        else:
            sign = "+" if delta >= 0 else ""
            text = f"Updated inventory: {item} {sign}{delta}."
        return OrchestratorReply(
            text=text,
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
        )

    def _handle_inventory_read(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch ``inventory.query`` / ``inventory.list_low`` to the read path.

        The read does its own structured state lookup (no retrieval bundle
        is needed for a numeric / list answer). The audit entry records the
        canonical state.yaml path so the user can reconstruct the answer
        offline.
        """
        from domains.inventory.handler import read as inventory_read

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        outcome = "ok"
        error_message: Optional[str] = None
        reply_text = ""
        mode: Optional[str] = None
        item: Optional[str] = None
        count: float = 0
        try:
            result = inventory_read(
                intent=intent,
                query=message,
                vault_root=self._vault_root,
                query_parser=self._inventory_query_parser,
                invoker=self._invoker,
            )
            reply_text = result.reply_text
            mode = result.mode
            item = result.item
            count = result.count
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        state_path = self._vault_root / "inventory" / "state.yaml"
        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "read",
            "actor": "kernel.orchestrator",
            "domain": "inventory",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
            "match_count": count,
            "path": str(state_path),
        }
        if mode is not None:
            entry["mode"] = mode
        if item:
            entry["item"] = item
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error":
            return OrchestratorReply(
                text="Sorry — something went wrong reading inventory.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        return OrchestratorReply(
            text=reply_text,
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
        )

    def _handle_fitness_write(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch fitness logging intents to the fitness plugin's write path.

        Wires:
          1. ``domains.fitness.handler.write`` -> appends the right JSONL
             (workouts / meals / metrics) and (for metric/profile updates)
             may rewrite ``profile.yaml`` + log a ``profile_event``.
          2. Audit-log a ``write`` op with ``domain=fitness`` and the path.
          3. Trigger the every-5-writes index refresh on a successful append.
          4. Reply with a brief confirmation the user can verify.

        Plan generation (``fitness.workout_plan`` / ``fitness.nutrition_plan``)
        is NOT routed here — that's issue #8.
        """
        # Lazy import keeps the kernel decoupled from plugin compile-time.
        from domains.fitness.handler import write as fitness_write

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        outcome = "ok"
        error_message: Optional[str] = None
        path: Optional[Path] = None
        appended = False
        row_id: Optional[str] = None
        try:
            result = fitness_write(
                intent=intent,
                message=message,
                session=session,
                vault_root=self._vault_root,
                clock=self._clock,
                extractor=self._fitness_extractor,
                invoker=self._invoker,
            )
            path = result.path
            appended = result.appended
            row_id = result.row_id
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "write",
            "actor": "kernel.orchestrator",
            "domain": "fitness",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
            "appended": 1 if appended else 0,
            "skipped": 0 if appended else 1,
        }
        if path is not None:
            entry["path"] = str(path)
        if row_id is not None:
            entry["row_id"] = row_id
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error":
            return OrchestratorReply(
                text="Sorry — something went wrong saving that fitness event.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        # One-line note about the fitness change for the running session.
        action = {
            "fitness.workout_log": "workout logged",
            "fitness.meal_log": "meal logged",
            "fitness.metric_log": "metric logged",
            "fitness.profile_update": "profile updated",
        }.get(intent, intent)
        session_update(
            session,
            f"fitness: {action}",
            vault_root=self._vault_root,
            clock=self._clock,
        )

        # Bump the writes-since-refresh counter only when a new row landed.
        if appended:
            self._maybe_refresh_index(started_ts=started_ts)

        if not appended:
            text = "Already on file — fitness event unchanged."
        else:
            text = f"Logged ({intent.split('.')[-1]})."
        return OrchestratorReply(
            text=text,
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
        )

    def _handle_fitness_query(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch ``fitness.query`` to the fitness plugin's read path.

        The fitness read does its own structured aggregation via
        ``query_fitness`` (no retrieval bundle is needed for a numeric
        answer). The audit entry records the intent + outcome.
        """
        from domains.fitness.handler import read as fitness_read

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        outcome = "ok"
        error_message: Optional[str] = None
        reply_text = ""
        try:
            reply_text = fitness_read(
                intent=intent,
                query=message,
                vault_root=self._vault_root,
                query_parser=self._fitness_query_parser,
                invoker=self._invoker,
            )
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "read",
            "actor": "kernel.orchestrator",
            "domain": "fitness",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
        }
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error":
            return OrchestratorReply(
                text="Sorry — something went wrong reading from your fitness log.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        return OrchestratorReply(
            text=reply_text,
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
        )

    def _handle_fitness_plan(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch ``fitness.workout_plan`` / ``fitness.nutrition_plan`` -> fitness.read.

        Plan generation runs the 7-step recipe in ``domains/fitness/prompt.md``
        and writes a markdown plan to ``vault/fitness/plans/``. The audit
        entry records ``op=read`` (the user's perspective is "the agent
        gave me a plan") with ``domain=fitness`` so downstream eval tools
        can attribute the work.
        """
        from domains.fitness.handler import read as fitness_read

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        outcome = "ok"
        error_message: Optional[str] = None
        reply_text = ""
        try:
            reply_text = fitness_read(
                intent=intent,
                query=message,
                vault_root=self._vault_root,
                invoker=self._invoker,
                clock=self._clock,
            )
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "read",
            "actor": "kernel.orchestrator",
            "domain": "fitness",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
        }
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error":
            return OrchestratorReply(
                text="Sorry — something went wrong generating that plan.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        return OrchestratorReply(
            text=reply_text,
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
        )

    def _handle_reminder_write(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch ``reminder.{add,add_when,cancel}`` -> reminder write path.

        Wires:
          1. ``domains.reminder.handler.write`` -> appends events.jsonl
          2. Audit-log a ``write`` op with ``domain=reminder`` and the path
          3. Reply with a brief confirmation
        """
        # Lazy import keeps the kernel decoupled from plugin compile-time.
        from domains.reminder.handler import write as reminder_write

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        outcome = "ok"
        error_message: Optional[str] = None
        path: Optional[Path] = None
        appended = False
        kind: Optional[str] = None
        try:
            result = reminder_write(
                intent=intent,
                message=message,
                session=session,
                vault_root=self._vault_root,
                clock=self._clock,
                extractor=self._reminder_extractor,
                invoker=self._invoker,
            )
            path = result.path
            appended = result.appended
            kind = result.kind
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "write",
            "actor": "kernel.orchestrator",
            "domain": "reminder",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
            "appended": 1 if appended else 0,
            "skipped": 0 if appended else 1,
        }
        if path is not None:
            entry["path"] = str(path)
        if kind is not None:
            entry["kind"] = kind
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error":
            return OrchestratorReply(
                text="Sorry — something went wrong saving that reminder.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        # Update the active session.
        action = {
            "reminder.add": "scheduled reminder",
            "reminder.add_when": "state-derived reminder",
            "reminder.cancel": "cancelled reminder",
        }.get(intent, intent)
        session_update(
            session,
            f"reminder: {action}",
            vault_root=self._vault_root,
            clock=self._clock,
        )

        if not appended:
            text = "Already on file — reminder unchanged."
        elif intent == "reminder.cancel":
            text = "Reminder cancelled."
        elif intent == "reminder.add_when":
            text = "Reminder scheduled (will fire when the condition becomes true)."
        else:
            text = "Reminder scheduled."
        return OrchestratorReply(
            text=text,
            tokens_in=0,
            tokens_out=0,
            duration_ms=duration_ms,
        )

    def _handle_reminder_list(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> OrchestratorReply:
        """Dispatch ``reminder.list`` -> reminder read path."""
        from domains.reminder.handler import read as reminder_read

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        outcome = "ok"
        error_message: Optional[str] = None
        reply_text = ""
        try:
            reply_text = reminder_read(
                intent=intent,
                query=message,
                vault_root=self._vault_root,
            )
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "read",
            "actor": "kernel.orchestrator",
            "domain": "reminder",
            "intent": intent,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
        }
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if outcome == "error":
            return OrchestratorReply(
                text="Sorry — something went wrong reading your reminders.",
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
            )

        return OrchestratorReply(
            text=reply_text,
            tokens_in=0,
            tokens_out=0,
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

    # -- index refresh ---------------------------------------------------

    def _refresh_state_path(self) -> Path:
        """Where the writes-since-last-refresh counter is persisted."""
        return self._vault_root / _REFRESH_STATE_RELATIVE

    def _read_refresh_state(self) -> dict:
        """Load the persistent state, returning a fresh dict if absent/corrupt.

        The state file is intentionally tiny and the read is cheap; we keep
        it on disk rather than in process memory so a kernel restart picks
        up the existing counter (issue #4: "writes since last refresh" is
        a vault-level invariant, not a process-level one).
        """
        state_path = self._refresh_state_path()
        if not state_path.exists():
            return {"writes_since_last_refresh": 0}
        try:
            raw = state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {"writes_since_last_refresh": 0}
            return data
        except (OSError, json.JSONDecodeError):
            return {"writes_since_last_refresh": 0}

    def _write_refresh_state(self, state: dict) -> None:
        """Persist the writes counter via ``vault.atomic_write`` so Drive sync stays happy."""
        # Lazy import keeps the module-level import graph small for tests
        # that monkeypatch around the kernel.
        from kernel.vault import atomic_write

        state_path = self._refresh_state_path()
        atomic_write(state_path, json.dumps(state, sort_keys=True) + "\n")

    def _refresh_threshold(self) -> int:
        """Pull the configured threshold; fall back to the default.

        Honors ``config.context_engineering.index_refresh_after_writes``
        first (matches ``configs/default.yaml``'s shape) then falls back
        to ``config.index_refresh_after_writes`` (a flat-shaped config
        helpful in tests) and finally to ``5``.
        """
        ce = (self._config or {}).get("context_engineering") or {}
        threshold = ce.get("index_refresh_after_writes")
        if threshold is None:
            threshold = (self._config or {}).get("index_refresh_after_writes")
        if threshold is None:
            return _DEFAULT_INDEX_REFRESH_AFTER_WRITES
        return max(1, int(threshold))

    def _maybe_refresh_index(self, *, started_ts: datetime) -> None:
        """Increment the write counter; refresh + audit-log + reset at threshold.

        Called after any successful vault write. The refresh runs inline
        and is audit-logged with ``op=index_refresh`` (carrying duration_ms
        and a few count-fields from ``RefreshResult``).

        A refresh failure is captured as ``outcome=error`` on the audit
        line; the counter is NOT reset on failure so the next write
        retries the refresh.
        """
        state = self._read_refresh_state()
        count = int(state.get("writes_since_last_refresh", 0)) + 1
        threshold = self._refresh_threshold()

        if count < threshold:
            self._write_refresh_state({"writes_since_last_refresh": count})
            return

        # Threshold hit — refresh inline and reset.
        wall_start = time.monotonic()
        outcome = "ok"
        error_message: Optional[str] = None
        files_indexed = 0
        clusters = 0
        tags = 0
        orphans = 0
        try:
            result = self._index_refresh(
                self._vault_root,
                config=self._config,
                clock=self._clock,
            )
            files_indexed = getattr(result, "files_indexed", 0)
            clusters = getattr(result, "clusters", 0)
            tags = getattr(result, "tags", 0)
            orphans = getattr(result, "orphans", 0)
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)

        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "index_refresh",
            "actor": "kernel.orchestrator",
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "writes_since_last_refresh": count,
            "files_indexed": files_indexed,
            "clusters": clusters,
            "tags": tags,
            "orphans": orphans,
        }
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        # Reset the counter only on a clean refresh; on error, leave the
        # counter at the threshold so the next write retries.
        if outcome == "ok":
            self._write_refresh_state({"writes_since_last_refresh": 0})
        else:
            self._write_refresh_state({"writes_since_last_refresh": count})
