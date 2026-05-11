# ADR 2026-05-09: Two-message Telegram pattern (immediate ack + result)

**Status:** Accepted
**Date:** 2026-05-09

## Context

Most kernel turns are fast: journal capture, finance append, inventory
adjust, and structured queries all return in well under a second. The
bridge in `kernel/telegram_bridge.py` originally awaited the
orchestrator and posted one reply via `message.reply_text` — a clean
one-shot contract that the eval harness and CLI also relied on.

Plan generation broke the assumption. `fitness.workout_plan` and
`fitness.nutrition_plan` route through `_handle_fitness_plan` at
`kernel/orchestrator.py:1105`, which shells out to `claude -p` for a
substantial generation pass — typically 10 to 30 seconds end-to-end.
During that window the bot was completely silent on Telegram. From
the user's seat the experience was indistinguishable from a hung
process: no acknowledgement that the request was received, no signal
that work was happening, no indication of when to expect a reply.

A typing indicator alone would technically satisfy "the bot is alive"
but it is generic — it does not confirm the bot understood *which*
request is being worked on. The desired behavior is a brief,
context-specific ack ("On it — pulling your last 14 days...") followed
by the actual result message when generation completes.

## Decision

`Orchestrator.handle_message` accepts an optional keyword-only
parameter `send_progress: Optional[Callable[[str], None]] = None`
(see `kernel/orchestrator.py:234`). Default `None` preserves the
one-shot reply contract that the eval harness, CLI, and tests use —
those callers don't have a chat surface to push progress to.

Long-running handlers — currently only `_handle_fitness_plan`, but
the contract generalizes to any future >5s handler — call
`self._send_progress("On it — pulling your last 14 days...")`
immediately on entry, then proceed with the slow work. The
orchestrator stashes the per-turn callback on `self._send_progress`
at the top of `handle_message` so dispatch helpers can pick it up
without threading the parameter through every internal signature.

The bridge wires the callback in `make_message_handler`
(`kernel/telegram_bridge.py:54`):

1. The bridge captures the running asyncio loop with
   `asyncio.get_running_loop()`.
2. It defines a synchronous `send_progress(text)` closure that calls
   `asyncio.run_coroutine_threadsafe(message.reply_text(text), loop)`.
3. It hands the orchestrator off to a thread executor via
   `loop.run_in_executor(None, lambda: orchestrator.handle_message(...))`
   so the LLM subprocess block does not stall the event loop.
4. The synchronous `send_progress` closure can therefore be safely
   called from inside the executor thread — it schedules the async
   `reply_text` back on the bot's loop without blocking the caller.

The result: the user sees two messages in chat — the immediate ack
followed by the final plan when it's ready.

## Alternatives considered

1. **Single, richer reply.** Wait for generation to finish, then send
   one well-formatted message. Rejected: the silence problem remains.
   10-30 seconds with no chat activity reads as a broken bot.

2. **Telegram typing indicator only.** Use `chat.send_chat_action("typing")`
   on a refresh loop while the LLM runs. Rejected: the indicator is
   generic. It confirms liveness but not intent. "Typing..." does not
   answer "did the bot understand that I asked for a workout plan, or
   is it about to ask me to clarify?" — the two-message pattern does.

3. **Streaming progress events.** Have the handler push intermediate
   status messages ("read profile", "summarizing last 14 days",
   "drafting plan", etc.). Rejected as over-engineering — the chat
   becomes noisy with intermediate spam, the user has to scroll past
   four messages to find the answer, and the kernel grows a
   streaming-events abstraction it doesn't otherwise need. One ack +
   one result is the minimum that solves the silence problem.

4. **Dedicated background-job worker** that posts results
   asynchronously via the bot API. Rejected as premature: it would
   require a new persistence layer for in-flight jobs, a
   reconciliation path for crashes mid-generation, and a separate
   process. Two messages from the same handler call is far simpler
   and meets the user need.

## Consequences

**Good**

- Plan-generation UX is now instant-feeling: ack lands in <100ms,
  result follows when ready.
- Eval and CLI callers are untouched — `send_progress=None` is the
  default, and the orchestrator's reply contract for those callers
  is byte-identical to before.
- The mechanism is general. Any future handler that exceeds a few
  seconds (cross-domain digest synthesis, reasoning-heavy retrieval)
  can opt in by calling `self._send_progress(...)` once on entry.
  No new kernel API needed.

**Bad / things to watch**

- **Concurrency posture changed.** The bridge now runs the
  orchestrator in a thread executor (`loop.run_in_executor`) instead
  of awaiting it inline. Two messages arriving close together would
  both be off-loop simultaneously. This is acceptable for two
  reasons that should remain true:
  1. The single-instance `flock` at `/tmp/personal-assistant.lock`
     (`kernel/orchestrator.py:53`, `SingleInstanceLock`) keeps only
     one bot process alive.
  2. Telegram delivers messages from one user's chat sequentially,
     and Jason is the sole user — so the natural traffic pattern is
     serialized. If the bot ever serves multiple users or message
     bursts arrive from a webhook upgrade, revisit this assumption
     and consider per-chat serialization.

- **Audit log shape unchanged.** Progress messages are
  user-experience scaffolding, not a vault event. They are *not*
  audit-logged. The audit entry for the turn is still written once,
  by the orchestrator, after the handler returns — preserving the
  invariant that one user message produces one audit entry per
  domain operation.

- **Test surface widens slightly.** Tests that need to assert on
  ack behavior pass a spy callable as `send_progress`. Tests that
  don't care continue to omit the parameter. Existing tests in
  `tests/test_claude_runner.py` and the orchestrator suite pass
  unchanged because the default is `None`.

- This is a kernel-touching change (signature on
  `Orchestrator.handle_message` plus the bridge's executor
  off-loading), but it adds an *opt-in capability* to all domains
  symmetrically — exactly the "cross-domain primitive" escape hatch
  CLAUDE.md calls out as a legitimate kernel change. No new
  domain-specific branch was added to the classifier or the
  dispatch table.
