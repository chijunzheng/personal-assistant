# ADR 2026-05-10: Hybrid chat context + auto-derived capabilities preamble

**Status:** Accepted
**Date:** 2026-05-10

## Context

Two adjacent failure modes in the live bot motivated this ADR. Both surface
in PRD #22 and both trace back to the same root cause: the kernel feeds the
LLM a turn-local view that is too thin to support the product's continuity
thesis.

**Failure 1 — "Yes, walk me through it" loses its referent.** The current
`_handle_echo` path in `kernel/orchestrator.py` builds a prompt from the
user's single inbound message plus retrieval hits. It does not include the
prior turn. A user reply of *"Yes, walk me through it"* arrives at the
orchestrator with no record of what *it* was, and the model either guesses
or asks the user to repeat themselves. This directly contradicts the
product thesis at `plans/personal-assistant-prd-20260429.md:21`:
*"Continuity — most chatbots forget across sessions; most note apps don't
reason."* The bot was forgetting *within* a session.

**Failure 2 — capability hallucination.** `kernel/prompts/system.md`
describes the kernel/plugin architecture to the LLM in some detail
(plugin recipe, eval runs, kernel-vs-plugin distinction). When asked
*"what can you do?"*, the LLM regurgitates that architecture as if it
were user-facing functionality — offering to walk the user through "the
plugin recipe" or "running the eval harness." None of those are things
the user can do through Telegram. The system prompt is leaking
implementation language into the user-facing surface.

The shared root cause: the kernel does not own a single, structured view
of *"what the user has been talking about"* and *"what this assistant can
actually do for the user."* Each handler reconstructs the first
ad-hoc; the second is a frozen artifact of whatever the system prompt
happens to say today.

`active_session.md` already exists as a compact rolling summary, but it is
lossy by design — it captures themes, not verbatim turns. *"Yes, walk me
through it"* needs the verbatim antecedent, not the theme.

## Decision

Two coordinated kernel changes, scoped tightly so neither becomes a
per-domain branch.

**1. Hybrid chat context, loaded once at the orchestrator boundary.**

A new module `kernel/chat_log.py` exposes two pure functions:

- `append(vault_root, turn)` — append a turn record to a daily-rotated
  `vault/_chat_log/YYYY-MM-DD.jsonl`. Content-hash IDs (sha256 of
  `chat_id|timestamp|role|text`), atomic writes via `kernel/vault.py`,
  same shape as `_audit/`.
- `load_recent(vault_root, n=5)` — return the last `n` turns across
  daily files, newest-last, for prompt embedding.

`Orchestrator.handle_message` calls `load_recent` exactly once per turn
and assembles a `ChatContext` value (last 5 turns verbatim + the existing
`active_session.md` summary text). That single value is passed to the
classifier and to every handler. Handlers do not re-load. The classifier
prompt at `kernel/prompts/classifier.md` gains a `{recent_turns}` slot;
`_handle_echo` and the structured handlers gain a `{chat_context}` slot
in their prompt assembly.

**2. Auto-derived capabilities preamble.**

A new module `kernel/capabilities.py` exposes a single pure function
`build_capabilities_preamble(domains_root) -> str`. It scans
`domains/*/domain.yaml`, reads two new optional fields per domain —
`description_user_facing` (one sentence the user would recognize) and
`intent_examples` (2-4 short example phrasings) — and renders a compact
markdown block.

`kernel/prompts/system.md` is scrubbed of architecture leakage (no more
"plugin recipe", "kernel question", "eval run") and gains a single
`{capabilities}` placeholder that the orchestrator substitutes at prompt
assembly time. Domain plugins that omit the new fields are silently
skipped from the preamble — the kernel never hardcodes a domain name.

The schema extension is additive and optional, so existing domains keep
working. The plugin contract in `domains/README.md` will be updated as
part of #25 to document the two new fields.

## Alternatives considered

**For chat context:**

1. **Pure compact summary (PRD-as-written).** Pass only
   `active_session.md`. Rejected because the summary is intentionally
   lossy — it loses the antecedent that *"Yes, walk me through it"*
   refers to. The failure mode that motivated the PRD would survive
   the fix.
2. **Full conversation history.** Pass everything since session start.
   Rejected as token-explosive — long sessions blow the prompt budget,
   classifier latency degrades, and most of the history is irrelevant
   to the current turn.
3. **Last-N turns only, no summary.** Drop `active_session.md` and rely
   on the verbatim window. Rejected because long sessions lose
   higher-level continuity ("we've been planning a Tokyo trip for an
   hour") that a 5-turn window can't carry.

**For capabilities:**

1. **Hardcoded list inside `system.md`.** Easiest change, one file.
   Rejected: violates the data-driven kernel rule from CLAUDE.md
   ("classifier reads YAML; add YAML, not code"). Adding fitness or
   recipes would require editing the kernel prompt — exactly the
   coupling the cardinal rule prohibits.
2. **No preamble, let the model infer from system.md prose.** Rejected:
   this is the status quo and produces the "plugin recipe"
   hallucination. Vague greetings have no anchor.

## Consequences

**Why this is not a cardinal-rule violation.** CLAUDE.md says *"kernel
never changes to add a use case."* Neither change here adds a use case.
Both are shared infrastructure that every existing and future domain
benefits from symmetrically: the chat context is read by the classifier
and all handlers without any per-domain branch; the capabilities
preamble is generated *from* the domains, not hardcoded *for* any of
them. New domains continue to register by dropping a YAML file — they
just get two new optional fields they may fill in.

**Good**

- *"Yes, walk me through it"* resolves to the prior turn deterministically.
- *"What can you do?"* answers from the live domain set, not from
  whatever the system prompt happened to say six commits ago.
- Adding a new domain still requires zero kernel edits; the new fields
  are optional and the preamble updates itself.

**Bad / things to watch**

- **Classifier blast radius.** Every turn classifies, so changes to
  `kernel/prompts/classifier.md` and the classifier's prompt assembly
  are high-risk. Mitigation: isolated tests in
  `tests/test_classifier_chat_context.py` covering the empty-history,
  short-history, and over-N-history cases before the prompt template
  itself changes.
- **Vault concurrency invariants preserved.** `vault/_chat_log/` follows
  the same conventions as `_audit/`, `_index/`, `_inbox/`: underscore
  prefix marks it as kernel-managed (not user-edited), atomic writes
  via `kernel/vault.py`, append-only with content-hash dedup, daily
  rotation. The 30-min user-edit buffer does not apply because users
  never edit chat-log files.
- **Prompt-token cost rises modestly per turn.** Five verbatim turns
  plus the summary plus the preamble adds bounded overhead — measured
  during #25 implementation, recorded in the eval harness so any
  regression is visible.
- **Sequencing.** The implementation lands across four issues — #25
  (kernel infrastructure: `chat_log.py`, `capabilities.py`,
  schema fields, `system.md` scrub), #26 (orchestrator wiring), #27
  (classifier wiring), #28 (handler-side adoption). #27 and #28 are
  sequenced behind #25 deliberately so each PR stays reviewable and
  the eval harness can isolate any regression to a single change.

This is a kernel-touching change but it adds shared infrastructure that
every domain consumes uniformly — the same escape hatch CLAUDE.md
acknowledges for the two-message Telegram pattern (ADR 2026-05-09).
No new branch in the classifier dispatch table, no new domain-specific
code path, no edit to `configs/baseline.yaml`.
