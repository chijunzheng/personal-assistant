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
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

from kernel import chat_log
from kernel.audit import write_audit_entry
from kernel.capabilities import build_capabilities_preamble
from kernel.chat_context import ChatContext, compose_recent_preamble
from kernel.claude_runner import ClaudeResponse, ClaudeRunnerError, invoke as claude_invoke
from kernel.classifier import Classifier, FALLBACK_INTENT
from kernel.index import refresh as index_refresh_default
from kernel.retrieval import gather_context
from kernel.session import Session, load_or_create as session_load, update as session_update

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LOCK_PATH",
    "InstanceLockError",
    "Orchestrator",
    "OrchestratorReply",
    "SingleInstanceLock",
]


DEFAULT_LOCK_PATH = Path("/tmp/personal-assistant.lock")
# Vault location is overridable via the ``VAULT_ROOT`` environment variable so
# the same code path serves dev (relative ``vault/``), production (iCloud
# Obsidian subfolder), and tests (tmp_path) without a config-file change.
# See docs/decisions/2026-05-09-vault-on-icloud.md.
DEFAULT_VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "vault"))
DEFAULT_AUDIT_ROOT = DEFAULT_VAULT_ROOT / "_audit"
DEFAULT_CHAT_ID = "default"
DEFAULT_SYSTEM_PROMPT = (
    "You are a concise personal-assistant tracer. Reply briefly and helpfully."
)
# Project-default domains root used to derive the user-facing capabilities
# preamble. Tests pass an explicit ``domains_root`` so this default only
# kicks in production. Substitution is one-shot at orchestrator construction
# (the result is deterministic for a given filesystem state).
DEFAULT_DOMAINS_ROOT = Path("domains")
# Substitution token in ``kernel/prompts/system.md`` that is replaced with
# the auto-derived capabilities preamble at orchestrator construction time.
# Kept here (not in ``kernel.capabilities``) because the substitution is the
# orchestrator's responsibility -- the capabilities module just renders the
# block.
_CAPABILITIES_PLACEHOLDER = "{capabilities}"

# Where the writes-since-last-refresh counter is persisted. Lives under
# ``vault/_index/`` next to INDEX.md so it shares the same Drive sync scope.
_REFRESH_STATE_RELATIVE = Path("_index") / ".refresh_state.json"

# Default threshold pulled from configs/default.yaml; the orchestrator
# accepts an override via ``config.context_engineering.index_refresh_after_writes``.
_DEFAULT_INDEX_REFRESH_AFTER_WRITES = 5

# Prefix that ``domains.fitness._plans.generate_plan`` returns on a
# successful plan write. The orchestrator strips it to recover the
# saved filename when leaving a session-update trace; refusal replies
# (no prefix match) intentionally bypass the session update so failed
# generations don't pollute active_session.md.
_FITNESS_PLAN_SAVED_PREFIX = "Saved plan to fitness/plans/"


def _compose_echo_prompt(*, message: str, recent: list) -> str:
    """Prepend a verbatim "Recent conversation" preamble to the user's message.

    Pure function so the echo-handler test can assert the composed prompt
    without driving the whole orchestrator. ``recent`` is a list of
    ``ChatTurn`` (chronological, newest last); when empty, the function
    returns the message unchanged so the issue-#1 prompt shape is
    preserved verbatim for fresh sessions.

    Delegates to ``kernel.chat_context.compose_recent_preamble`` so the
    rendering stays consistent with every other handler that folds
    chat-context into its prompt (journal-query, fitness-plan).
    """
    preamble = compose_recent_preamble(recent)
    if not preamble:
        return message
    return f"{preamble}{message}"


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
        domains_root: str | os.PathLike[str] = DEFAULT_DOMAINS_ROOT,
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
        # Capabilities substitution: replace the ``{capabilities}`` token in
        # the supplied system prompt with the auto-derived preamble. Done
        # once at construction time -- the result is deterministic for a
        # given filesystem state (ADR 2026-05-10), so per-turn re-rendering
        # would just burn cycles. Substitution is a no-op for system prompts
        # that don't contain the token (e.g., the legacy tracer default), so
        # callers passing a hand-written prompt aren't surprised.
        # ``claude_runner.invoke`` itself is unchanged: substitution belongs
        # at the kernel boundary that knows about ``domains_root``, not in
        # the public LLM-runner API.
        self._domains_root = Path(domains_root)
        self._system_prompt = self._substitute_capabilities(system_prompt)
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
        # Per-turn ChatContext — populated at the top of every
        # ``handle_message`` call, consumed by dispatch helpers.
        # Seeded with the empty default so any helper that runs outside
        # a ``handle_message`` boundary (e.g., a future reminder
        # dispatcher reusing the orchestrator) sees a usable value
        # rather than ``AttributeError``.
        self._chat_context: ChatContext = ChatContext()
        # Mid-turn progress sender; set per-turn by ``handle_message``
        # from the Telegram bridge. ``None`` for eval/CLI callers and for
        # tests that call ``_handle_*`` directly without going through
        # ``handle_message``.
        self._send_progress: Optional[Callable[[str], None]] = None

    def _substitute_capabilities(self, system_prompt: str) -> str:
        """Replace ``{capabilities}`` in the supplied system prompt.

        Done once at construction time. The capabilities preamble is
        deterministic for a given ``domains_root`` filesystem state, so a
        per-turn re-render would just burn cycles. Returns the prompt
        unchanged when the placeholder is absent so callers passing a
        hand-written prompt (or the legacy tracer default) aren't surprised.
        """
        if _CAPABILITIES_PLACEHOLDER not in system_prompt:
            return system_prompt
        preamble = build_capabilities_preamble(self._domains_root)
        return system_prompt.replace(_CAPABILITIES_PLACEHOLDER, preamble)

    def start(self) -> None:
        """Acquire the single-instance lock — call once at process start."""
        self._lock.acquire()

    def stop(self) -> None:
        """Release the single-instance lock — call once at process shutdown."""
        self._lock.release()

    def handle_message(
        self,
        message: str,
        *,
        send_progress: Optional[Callable[[str], None]] = None,
    ) -> OrchestratorReply:
        """Run one turn: classify, dispatch, audit, reply.

        Per the ADR (chat-context section), the orchestrator loads a
        ``ChatContext`` (verbatim recent turns + the active-session
        summary) ONCE at the top of the turn and stashes it on
        ``self._chat_context`` so the dispatch helpers pick it up
        without threading the value through every signature. After
        dispatch (success OR error), a single ``chat_log.append`` at
        the bottom records the turn — that consolidation means every
        handler benefits from chat-context continuity, not just the
        echo fallback (issue #27).

        ``send_progress`` is the bridge-supplied callable that schedules
        an immediate Telegram reply on the bot's event loop. Long-running
        handlers (plan generation) push a coach-voice ack via this so
        the user knows the bot heard them while the 10-30s LLM call is
        in flight. Eval / CLI callers pass ``None`` (the default).
        """
        started_ts = self._clock()
        wall_start = time.monotonic()
        # Stash so dispatch helpers can pick it up without threading the
        # parameter through every signature.
        self._send_progress = send_progress

        # Step 0: assemble the per-turn ChatContext exactly once. Both
        # sub-loads degrade gracefully — a missing chat log or a fresh
        # session yields an empty context rather than dropping the
        # user's message. The classifier wiring (#28) will pick this up
        # via ``self._chat_context``; the seam is left ready here.
        self._chat_context = self._load_chat_context()

        # Step 1: classify (if a classifier is wired). With no classifier we
        # preserve the issue-#1 echo path verbatim. The per-turn ChatContext
        # is threaded into the classifier so referential follow-ups
        # ("yes, walk me through it") resolve to a real domain intent
        # instead of falling through to FALLBACK_INTENT (issue #28).
        intent = self._classify(
            message, started_ts, chat_context=self._chat_context
        )

        # Step 2: dispatch on the classified intent. Per-intent helpers
        # pick up the per-turn ``self._chat_context`` directly so we
        # don't have to thread it through every signature.
        reply = self._dispatch(
            intent=intent,
            message=message,
            started_ts=started_ts,
            wall_start=wall_start,
        )

        # Step 3: consolidated chat-log append. Every successful or
        # errored turn produces one log entry so the next turn's
        # ChatContext sees the antecedent — including the friendly
        # apology when dispatch raised. Failures here are non-fatal:
        # the user's reply path must not break because the log file
        # could not be written.
        try:
            chat_log.append(
                self._chat_id,
                user_msg=message,
                bot_reply=reply.text,
                intent=intent,
                vault_root=self._vault_root,
                clock=self._clock,
            )
        except Exception:  # noqa: BLE001
            logger.exception("chat_log append failed; continuing")

        return reply

    def _dispatch(
        self,
        *,
        intent: str,
        message: str,
        started_ts: datetime,
        wall_start: float,
    ) -> "OrchestratorReply":
        """Route ``intent`` to the right ``_handle_*`` helper and return its reply.

        Pulled out of ``handle_message`` so the per-turn boundary
        (load ChatContext -> classify -> dispatch -> append chat log)
        reads top-down without a hundred-line if/elif chain in the
        middle. Dispatch table:

          - ``journal.query``      -> journal read (retrieval + LLM)
          - ``journal.*``          -> journal write (capture path)
          - ``finance.query``      -> finance read (structured query_finance)
          - ``finance.transaction``-> finance write (extract + append)
          - ``inventory.{add,consume,adjust}`` -> inventory write
          - ``inventory.{query,list_low}``    -> inventory read
          - ``fitness.{workout_log,meal_log,metric_log,profile_update}``
                                  -> fitness write (logging surface)
          - ``fitness.query``      -> fitness read (query_fitness)
          - ``fitness.{workout,nutrition}_plan`` -> fitness plan generation
          - ``reminder.{add,add_when,cancel}``  -> reminder write
          - ``reminder.list``      -> reminder read
          - everything else        -> generic echo (issue #1 fallback)
        """
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

    def _load_chat_context(self) -> ChatContext:
        """Assemble the per-turn ``ChatContext`` from chat log + active session.

        Both reads degrade gracefully — a missing chat log or a fresh
        session yields empty fields rather than raising. This is the
        sole I/O entry point for chat-context per turn (every dispatch
        helper consumes ``self._chat_context``); centralizing it here
        is what makes the ADR's "load once" discipline enforceable.
        """
        try:
            recent = chat_log.load_recent(
                self._chat_id, n=5, vault_root=self._vault_root
            )
        except Exception:  # noqa: BLE001
            logger.exception("chat_log load_recent failed; using empty context")
            recent = []

        try:
            session = session_load(
                self._chat_id,
                vault_root=self._vault_root,
                clock=self._clock,
            )
            summary = session.summary or ""
        except Exception:  # noqa: BLE001
            logger.exception("session load failed; using empty summary")
            summary = ""

        return ChatContext(
            recent_turns=tuple(recent),
            session_summary=summary,
        )

    def _classify(
        self,
        message: str,
        started_ts: datetime,
        *,
        chat_context: Optional[ChatContext] = None,
    ) -> str:
        """Run the classifier and audit-log the result. Returns the intent.

        ``chat_context`` is threaded through to ``Classifier.classify`` so
        referential follow-ups can resolve against verbatim antecedents
        (issue #28). Defaults to ``None`` so any future caller wiring
        bypassing ``handle_message`` (e.g., a reminder dispatcher) still
        gets the pre-#28 behavior without surprises.
        """
        if self._classifier is None:
            return ""  # echo path; no classification performed

        wall_start = time.monotonic()
        outcome = "ok"
        error_message: Optional[str] = None
        intent = FALLBACK_INTENT
        try:
            intent = self._classifier.classify(
                message, chat_context=chat_context
            )
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

        # Per ADR + issue #27: fold the recent verbatim turns into the
        # journal-read prompt so referential follow-ups ("yes, the
        # earlier note", "what was that one about Tokyo?") resolve
        # against actual antecedents instead of guessing from
        # retrieval hits alone.
        chat_preamble = compose_recent_preamble(
            self._chat_context.recent_turns
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
                chat_context_preamble=chat_preamble,
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

        Referential follow-up (PRD #22): if ``classifier-with-context``
        routed something like "Walk me through it" here right after a
        plan write, structured aggregation is useless — the user wants
        the bot to walk through the recently-saved plan. We detect this
        shape (short message + walkthrough keywords + a "plan saved at
        ..." trace in ``active_session.md``) and reroute via the LLM.
        """
        from domains.fitness.handler import read as fitness_read

        session = session_load(self._chat_id, vault_root=self._vault_root, clock=self._clock)

        if _looks_like_walkthrough_followup(message):
            plan_rel = _extract_recent_plan_path(session.summary)
            if plan_rel:
                walkthrough = self._walk_through_recent_plan(
                    message=message,
                    plan_rel_path=plan_rel,
                    started_ts=started_ts,
                    wall_start=wall_start,
                    session=session,
                )
                if walkthrough is not None:
                    return walkthrough

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

    def _walk_through_recent_plan(
        self,
        *,
        message: str,
        plan_rel_path: str,
        started_ts: datetime,
        wall_start: float,
        session: Session,
    ) -> Optional[OrchestratorReply]:
        """LLM walkthrough of a recently-saved plan.

        Reads the plan file under ``self._vault_root``, hands it to the
        invoker with the chat-context preamble + a short instruction to
        walk Jason through it in coach voice. Returns ``None`` if the
        plan file is missing — the caller falls back to structured query.
        """
        plan_path = self._vault_root / plan_rel_path
        if not plan_path.exists():
            return None
        try:
            plan_body = plan_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        chat_preamble = compose_recent_preamble(self._chat_context.recent_turns)
        prompt = (
            f"{chat_preamble}"
            f"Jason just said: {message!r}.\n\n"
            f"Walk him through the plan below in coach voice (warm, concise, "
            f"phone-readable). Highlight 2-3 things he should pay attention to "
            f"first. End with one question that helps him commit to action. "
            f"Do NOT dump the plan back at him verbatim — synthesize.\n\n"
            f"## Plan at {plan_rel_path}\n\n{plan_body}\n"
        )

        outcome = "ok"
        error_message: Optional[str] = None
        response: Optional[ClaudeResponse] = None
        try:
            response = self._invoker(prompt, system_prompt=self._system_prompt)
        except ClaudeRunnerError as err:
            outcome = "error"
            error_message = str(err)

        duration_ms = int((time.monotonic() - wall_start) * 1000)
        entry: dict[str, object] = {
            "ts": started_ts.isoformat(),
            "op": "read",
            "actor": "kernel.orchestrator",
            "domain": "fitness",
            "intent": "fitness.plan_walkthrough",
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": self._config_label,
            "session_id": session.session_id,
            "path": str(plan_path),
        }
        if response is not None:
            entry["tokens_in"] = response.tokens_in
            entry["tokens_out"] = response.tokens_out
        if error_message is not None:
            entry["error"] = error_message
        self._audit_writer(entry, audit_root=self._audit_root)

        if response is None:
            return OrchestratorReply(
                text="Sorry — couldn't walk through that plan just now.",
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

        # Per ADR + issue #27: fold recent verbatim turns into the
        # plan-generation prompt. End-to-end behavior the issue wants:
        # *"now do a workout one too"* after a nutrition-plan request
        # carries the prior plan's framing into the new generation.
        chat_preamble = compose_recent_preamble(
            self._chat_context.recent_turns
        )

        # Coach-voice ack so the user knows we heard them while the
        # 10-30s LLM call is in flight. Defensive: send_progress is None
        # for eval/CLI callers and may raise if the bridge is unhealthy.
        if self._send_progress is not None:
            try:
                self._send_progress(_fitness_plan_ack(intent))
            except Exception:  # noqa: BLE001
                logger.warning("send_progress failed for fitness plan ack", exc_info=True)

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
                chat_context_preamble=chat_preamble,
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

        # Leave a one-line trace in the active session ONLY on a real
        # plan write. Refusal shapes (reply doesn't start with the saved
        # prefix — e.g., "Cannot generate a plan — ...") mean nothing
        # was written, so the session stays untouched. Without this,
        # generated plans are invisible to downstream session-aware
        # features (PRD #22 / issue #24). NOTE: this is the issue-#24
        # fix — preserved verbatim per #27's instructions.
        if reply_text.startswith(_FITNESS_PLAN_SAVED_PREFIX):
            relative_path = "fitness/plans/" + reply_text[
                len(_FITNESS_PLAN_SAVED_PREFIX):
            ].rstrip(".")
            kind = "workout" if intent == "fitness.workout_plan" else "nutrition"
            session_update(
                session,
                f"{kind} plan saved at {relative_path}",
                vault_root=self._vault_root,
                clock=self._clock,
            )

        return OrchestratorReply(
            text=_warm_fitness_plan_reply(intent=intent, raw_reply=reply_text),
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
        """Issue-#1 fallback: invoke the runner, log, return its text.

        Per the ADR at ``docs/decisions/2026-05-10-chat-context-and-capabilities.md``,
        the echo path folds the recent verbatim turns into the LLM
        prompt as a "Recent conversation" preamble — that's what lets
        *"yes, walk me through it"* resolve to its antecedent.

        Issue #25 wired chat_log into the echo path only; issue #27
        generalized the wiring so every handler benefits. The recent
        turns are now read off ``self._chat_context`` (loaded once per
        turn at the top of ``handle_message``) and the per-turn append
        is consolidated into ``handle_message`` so this handler stays
        focused on the LLM call itself.
        """
        prompt = _compose_echo_prompt(
            message=message,
            recent=list(self._chat_context.recent_turns),
        )

        outcome = "ok"
        error_message: Optional[str] = None
        response: Optional[ClaudeResponse] = None

        try:
            response = self._invoker(prompt, system_prompt=self._system_prompt)
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
            # Friendly apology preserves continuity for the next turn —
            # the consolidated chat_log.append in ``handle_message``
            # records "user asked X, the bot apologized."
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


# ---------------------------------------------------------------------------
# Coach-voice helpers for fitness plan generation.
#
# The bare ``"Saved plan to fitness/plans/<file>.md."`` string returned by
# ``domains.fitness._plans.generate_plan`` reads like a CRUD app. These
# helpers (1) push an immediate ack via the bridge's ``send_progress``
# callback so the user knows the bot heard them while the 10-30s LLM call
# is in flight, and (2) warm the final reply into coach voice.
# ---------------------------------------------------------------------------


def _fitness_plan_ack(intent: str) -> str:
    """Return the coach-voice ack text for the given plan intent."""
    if intent == "fitness.workout_plan":
        return "Yes! Pulling your last 14 days of training and recent journal notes — one sec… 💪"
    return "On it! Checking your meals, weight trend, and what's in the pantry — one sec… 🥗"


def _warm_fitness_plan_reply(*, intent: str, raw_reply: str) -> str:
    """Wrap the bare 'Saved plan to ...' string in coach voice.

    Refusal replies (anything not starting with the saved-prefix — e.g.,
    "Cannot generate a plan — ..." when the profile has TODO holes) pass
    through unchanged so the user sees the actionable original.
    """
    if not raw_reply.startswith(_FITNESS_PLAN_SAVED_PREFIX):
        return raw_reply
    filename = raw_reply[len(_FITNESS_PLAN_SAVED_PREFIX):].rstrip(".")
    kind = "workout" if intent == "fitness.workout_plan" else "nutrition"
    return (
        f"Done! 🎯 Saved your {kind} plan to fitness/plans/{filename} — "
        "open it in Obsidian to take a look. Want me to walk you through it?"
    )


# ---------------------------------------------------------------------------
# Walkthrough detection — when the user says "walk me through it" / "tell me
# more" / "yes" right after a plan was generated, ``classifier-with-context``
# (#28) routes this to ``fitness.query``. But that handler runs structured
# aggregation, which is useless for a walkthrough request. These helpers let
# the query handler detect referential follow-ups and reroute through an LLM
# read of the just-saved plan file.
# ---------------------------------------------------------------------------


_WALKTHROUGH_KEYWORDS = (
    "walk me through",
    "tell me more",
    "explain",
    "talk me through",
    "go on",
    "yes",
    "yeah",
    "sure",
    "ok",
    "okay",
    "please",
    "what's in it",
    "what is in it",
    "break it down",
)


def _looks_like_walkthrough_followup(message: str) -> bool:
    """Heuristic: is this a short referential follow-up rather than a query?

    Short (< 60 chars) + contains a walkthrough keyword OR is purely a yes-shape.
    """
    text = message.strip().lower()
    if not text or len(text) > 60:
        return False
    return any(kw in text for kw in _WALKTHROUGH_KEYWORDS)


def _extract_recent_plan_path(session_summary: str) -> Optional[str]:
    """Return the last 'plan saved at <relative_path>' note from the session, if any.

    Session-summary lines from issue #24 look like
    ``- nutrition plan saved at fitness/plans/2026-05-11-...md``. We walk the
    body bottom-up and return the most recent match.
    """
    for line in reversed(session_summary.splitlines()):
        stripped = line.strip().lstrip("- ").strip()
        marker = " plan saved at "
        if marker in stripped:
            return stripped.split(marker, 1)[1].strip()
    return None
