# Personal Assistant — PRD

**Date:** 2026-04-29
**Status:** Design phase complete; ready for implementation
**Source:** Multi-turn grilling session that produced the artifacts in
`README.md`, `CLAUDE.md`, `IMPLEMENTATION.md`, `kernel/*.md`,
`domains/*/domain.yaml`, `configs/{default,baseline}.yaml`, and
`eval/seed_queries/00-jason-seed-queries.md`.

---

## Problem Statement

Jason needs a single mobile-first interface that captures life signal
(thoughts, finance, household state, fitness, reminders) into a memory store
he can both query through an agent and read directly in Obsidian. Off-the-shelf
options fail on at least one of three axes:

- **Continuity** — most chatbots forget across sessions; most note apps don't reason.
- **Cost** — embedding-based RAG over a personal vault adds per-query inference cost that doesn't pay off at single-user scale.
- **Silos** — finance apps don't see workouts, fitness apps don't see journals, none of them see each other; signal that should compound is fragmented.

Jason also needs this to be a **portfolio artifact** that demonstrates
context-and-memory engineering competency for an AI-platform-engineer role
search — meaning the system must produce measurable, defensible claims about
why the engineering choices matter.

---

## Solution

A two-layer agentic system with a **hard architectural seam between an
unchanging kernel and replaceable domain plugins**. Memory is a plain
markdown/JSONL/YAML vault on Google Drive readable by Obsidian on any device.
The agent is `claude -p` headless, invoked once per turn. Retrieval is
**pure agentic tool calling** — no embeddings, no vector DB — using filesystem
primitives plus a kernel-maintained `INDEX.md` that seeds keyword expansion.

The portfolio thesis the system is engineered to demonstrate:

> *Meticulous context engineering produces equal-or-better answers using
> equal-or-fewer tokens than a vanilla agentic baseline — with no embeddings
> and no vector DB.*

This thesis is operationalized as an eval matrix: eight Boolean toggles in
`configs/default.yaml` (engineered, all ON) versus `configs/baseline.yaml`
(vanilla, all OFF). Both configs run head-to-head on the same real
interactions and are scored on five Likert dimensions plus a token-budget
axis.

---

## User Stories

### Mobile capture & interface

1. As a busy person on the go, I want to text a single Telegram bot from my phone, so that capturing thought, expense, inventory change, workout, or reminder requires zero context switching.
2. As a user, I want the bot to classify my message automatically (no `/journal`, `/finance` slash commands), so that the interface is conversational and I never have to remember a command.
3. As a user, I want to attach photos (statements, food, whiteboards), so that I can capture data that would be tedious to type.
4. As a user, I want concise but informative replies, so that I can read them on a phone screen without scrolling.
5. As a user, I want every interaction to be available later in Obsidian on my laptop, so that the mobile interface is a writer and the desktop interface is a reader without duplication.

### Journaling & learning capture

6. As a thinker, I want to dump unstructured thoughts and have them filed automatically into a dated markdown note, so that capture has zero overhead.
7. As a learner, I want the system to suggest `[[wikilinks]]` to related past notes, so that ideas connect over time without manual tagging.
8. As a researcher, I want to ask "what did I think about <X> last month?" and get back the actual entries, so that my own past becomes searchable.
9. As an Obsidian user, I want notes to be plain markdown with sane frontmatter, so that I'm never locked into the agent's interface.
10. As a user, I want a weekly digest of "topics you've been thinking about", so that emerging themes surface ambiently.

### Finance tracking

11. As a user, I want to upload a credit-card or bank statement PDF/image and have transactions extracted to structured rows, so that I don't manually transcribe.
12. As a user, I want to ask "how much did I spend on coffee last month?" and get a real summed number, so that I trust the answer over an LLM that hand-sums numbers in prose.
13. As a user, I want recurring charges flagged automatically as subscriptions, so that subscription creep is visible.
14. As a user, I want a spending breakdown in the weekly digest, so that I have ambient awareness without opening a finance app.
15. As a user, I want re-uploading the same statement to *not* duplicate transactions, so that idempotency is automatic.

### Inventory tracking

16. As a household manager, I want to text "bought 2 milks at Costco" and have inventory updated, so that tracking household state has no dedicated app.
17. As a household manager, I want to text "what's running low?" and get a real list, so that grocery shopping is informed.
18. As a household manager, I want low-stock alerts in the daily digest, so that I see depleting items before they run out.
19. As a household manager, I want a state-derived reminder ("remind me when I run out of AAA") to fire automatically when the condition becomes true, so that follow-ups don't slip.

### Fitness — workouts, nutrition, plans

20. As an athlete, I want to text my workouts in natural language ("did 5x5 squat at 100kg, 3x10 RDL, 20min easy bike") and have them structured into a normalized log, so that logging is fast.
21. As a calorie-conscious eater, I want food logging with calorie/macro estimation, so that nutrition tracking doesn't require a separate database app.
22. As a tracker, I want body metrics (weight, sleep, mood, soreness, energy, HRV) tracked over time, so that recovery and progress are visible.
23. As a trainee, I want workout plans generated from my actual recent training history, so that suggestions are personalized — not generic split templates.
24. As a trainee, I want nutrition plans that respect my dietary restrictions and current pantry inventory, so that plans are actionable today, not aspirational.
25. As a trainee, I want plans to adapt to subjective context from my journal (energy mentions, sleep complaints, life-stress), so that the assistant "knows" what's going on without me re-explaining it every time.
26. As a trainee, I want to update my profile (goal, target weight, restrictions, equipment) and have downstream macro targets recompute automatically, so that I edit one source of truth.
27. As a trainee, I want to ask "how was my training in March?" and get a real numeric answer with cited context (e.g., a 4-day vacation gap explained), so that trend queries are grounded.

### Reminders

28. As a user, I want to schedule reminders ("remind me Sunday 6pm to call mom"), so that follow-ups happen.
29. As a user, I want state-derived reminders ("remind me when X condition is true"), so that the assistant fires at the right *moment*, not at a guessed time.
30. As a user, I want reminder firing to be reliable within a few minutes, so that I trust the system enough to use it for things that matter.
31. As a user, I want reminders auto-spawned by other domains (e.g., inventory's "running low") to be unified with my manually-added reminders in one dispatcher, so that the system has one mental model for "things to ping me about."

### Proactive layer (digests, suggestions)

32. As a user, I want a daily 8am digest with today's plan, low-stock items, and calorie pacing, so that I start the day with relevant context.
33. As a user, I want a weekly Sunday digest reviewing the week and surfacing patterns, so that I get periodic ambient feedback.
34. As a user, I want digests to include "suggested actions" — things I should notice or do — not just data dumps, so that the assistant is an advisor, not a reporter.
35. As a user, I want the weekly digest to surface ambiguous classifications staged in the inbox, so that the safety-net entries get triaged regularly.

### Cross-domain reasoning

36. As a user, I want fitness plans to consult journal entries for subjective context, so that personalization is real, not template-based.
37. As a user, I want nutrition plans to consult inventory state, so that suggestions are grounded in what food is actually available.
38. As a user, I want spending breakdowns to optionally inform fitness nutrition adherence, so that signals across domains compound when relevant.
39. As a user, I want cross-domain reads to never violate plugin isolation (no plugin importing another), so that the architecture stays clean as it grows.

### Drive sync resilience

40. As a user, I want my vault editable from any device — phone Obsidian, laptop Obsidian, Mac mini — without the agent corrupting my edits, so that the vault is a collaborative human + agent space.
41. As a user, I want Drive's `* (Conflict *).md` files detected and resolved (merged or staged) automatically, so that I don't lose data and don't have to manually clean up.
42. As a user, I want a 30-minute buffer where the agent doesn't touch files I just edited, so that my in-flight work isn't overwritten.
43. As a user, I want every operation auditable, so that if anything goes wrong I can reconstruct what happened from the log.
44. As a user, I want every append idempotent on a content-derived id, so that Drive sync replays don't duplicate entries.

### Future-proofing & plugin model

45. As an evolving user, I want to add new use cases (e.g., reading list, contacts, projects) without modifying the kernel, so that the system grows without becoming brittle.
46. As the developer, I want every Claude Code session entering this repo to *know* not to edit kernel files when adding features, so that future agentic edits don't drift the architecture.
47. As the developer, I want each plugin to ship its own eval cases as a precondition for promotion, so that adding a domain comes with quality verification baked in.
48. As the developer, I want the classifier to discover new domains automatically by reading their YAML, so that adding a domain requires zero kernel-code change.

### Eval & portfolio claim

49. As a portfolio creator, I want to demonstrate the thesis that meticulous context engineering produces measurably better outputs than a vanilla agentic baseline, so that the project has a defensible claim.
50. As an evaluator, I want head-to-head comparison on real interactions across five dimensions (accuracy, grounding, conciseness, connection, trust), so that "better" is grounded.
51. As an evaluator, I want token budget tracked per turn from `claude -p` usage telemetry, so that I can show "equal-or-better quality at lower budget."
52. As a portfolio creator, I want a real-world Drive-conflict resolution example in the writeup, so that sync resilience is demonstrated, not just claimed.
53. As an evaluator, I want each engineering decision implementable as a single Boolean config flag, so that ablation is mechanical and clean.

### Audit & observability

54. As a user, I want every classify/write/read/digest/reminder/conflict-resolve operation logged, so that the system is debuggable.
55. As an evaluator, I want token telemetry parsed from each `claude -p` response, so that the eval comparison is data-driven.
56. As a user, I want audit logs rotated daily and append-only, so that they double as a recovery mechanism.

---

## Implementation Decisions

### Architecture

**Two-layer system with a hard seam.** The kernel never changes when adding
a use case. New use cases are entire directories under `domains/`. The
classifier and orchestrator are data-driven — they read each domain's YAML
at startup and route accordingly. This is the cardinal rule.

The architectural enforcer is `CLAUDE.md` at the project root, which
auto-loads in every Claude Code session and explicitly forbids editing
kernel files when adding features. Worked example walkthrough (the
"add fitness tracking" recipe) is included so future agentic edits follow
the plugin recipe rather than branching kernel code.

### Retrieval — pure agentic, no embeddings

**The agent navigates the vault using filesystem primitives** (`read_file`,
`grep`, `list_dir`) plus an LLM-driven keyword-expansion tool. There is no
vector DB and no embedding step. The vault's `_index/INDEX.md` (auto-maintained
TOC with topic clusters, synonyms, tag map, recent activity, vocabulary
frontier) is the seed for keyword expansion — every retrieval starts with an
INDEX read.

This is the central architectural bet: cheap structural retrieval over a
well-shaped vault beats expensive semantic retrieval at single-user scale.
An embedding fallback is documented as an escape hatch (`kernel/RETRIEVAL.md`)
if the eval ever shows a gap, but is not implemented in v1.

### Eight engineering Booleans (the eval matrix)

Each maps to a single Boolean in `configs/default.yaml` (ON) and
`configs/baseline.yaml` (OFF). Both configs share the same token budget for
fair comparison.

1. **`tiered_retrieval`** — ordered tool palette: index → session → targeted vault reads.
2. **`per_domain_shaping`** — domain plugins expose structured-query tools (e.g., `query_finance`, `query_fitness`) so aggregations don't fall back to LLM hand-summing of raw JSONL.
3. **`recency_weighting`** — agent prompted to prefer recent files; filename dates make this free.
4. **`active_session_summary`** — `vault/_index/active_session.md` carries a compact running summary across turns; agent always loads first.
5. **`vault_index_first`** — `INDEX.md` is read before any grep, seeding keyword expansion.
6. **`backlink_expansion`** — after reading a vault file, the agent harvests `[[wikilinks]]` and reads 1-hop neighbors when budget allows.
7. **`suggested_actions`** — the proactive layer's LLM pass over digests adds "what should you notice or do" advice rather than pure data dumps.
8. **`conflict_auto_merge`** — engineered config can LLM-merge Drive `* (Conflict *).md` files; baseline only stages and notifies.

### Concurrency invariants (non-negotiable for kernel code)

1. **Atomic writes only.** Every vault write goes through `atomic_write` (tmp + `os.replace`). Direct `open(path, 'w')` on vault files is forbidden.
2. **Event logs are append-only with sha256 ids.** Never edit or rewrite an event-log JSONL. Readers dedupe by `id`.
3. **Respect the 30-minute user-edit buffer.** Before modifying any narrative `.md`, check mtime; if within buffer, fall back to create-new or stage-to-pending.
4. **No vault write without an audit-log entry.** Audit precedes the write being considered "done."
5. **Single-agent assumption.** Kernel runs on exactly one machine; a startup flock prevents double-bot races.

### Audit log schema

One append-only JSONL file per day at `vault/_audit/YYYY-MM-DD.jsonl`.

- **Required fields:** `id` (sha256), `ts` (ISO8601 with tz), `op` (classify|write|read|digest_send|reminder_fire|conflict_resolve), `actor`, `outcome`, `duration_ms`, `config` (default|baseline).
- **Optional fields:** `domain`, `telegram_msg_id`, `session_id`, `intent`, `path`, `sha256_after`, `tokens_in`, `tokens_out`, `error`, `preview` (first ~200 chars of write content).

Every `claude -p` invocation logs `tokens_in` / `tokens_out` from the response's
`usage` field. No exceptions. The eval harness aggregates these from the
audit logs to produce the per-turn token chart.

### Hybrid storage strategy

The right storage shape depends on the access pattern.

- **Markdown narrative** for journal entries and generated plans — human-readable, Obsidian-native, links via `[[wikilinks]]`.
- **JSONL append-only with sha256 ids** for event-time data: transactions, workouts, meals, body metrics, reminders, profile-change events. Idempotency is automatic; deduplication is unnecessary in readers.
- **YAML for canonical mutable state** that's recomputable from event logs: inventory `state.yaml`, fitness `profile.yaml`, finance budgets/categories. Convenience reads, not source of truth.

### Five domain plugins on day one

- **journal** — narrative markdown, single intent pair (capture/query), weekly digest contributor.
- **finance** — JSONL transactions, YAML budgets/categories, structured `query_finance` tool, weekly digest contributor.
- **inventory** — hybrid YAML state + JSONL events, structured `query_inventory` tool, daily digest contributor (low-stock).
- **reminder** — JSONL events with `kind: scheduled | state_derived`, dedicated cron contract (no digest), unifies scheduled + state-derived dispatching.
- **fitness** — hybrid storage (profile.yaml + workouts/meals/metrics/profile_events JSONLs + plans markdown), structured `query_fitness` tool, daily + weekly digest contributor, declares cross-domain reads from journal/finance/inventory at plan-generation time.

### Cross-domain reads via kernel

Plugins **never import each other.** When fitness plan-generation needs
journal context, it does so through the filesystem tools the kernel grants
(`grep` over `vault/journal/`). Each plugin's `domain.yaml` declares its
`cross_domain_signals` so the kernel knows which other domains to seed
retrieval scope with for that intent.

### Drive sync — five layered defenses

1. Atomic writes (tmp + `os.replace`).
2. Event logs are append-only with sha256 ids; commutative-replay-safe.
3. 30-minute user-edit buffer; agent stays out of files the user has touched.
4. Conflict-watcher daemon scans every 1 minute for `* (Conflict *).md` files; engineered config LLM-merges, baseline stages to `vault/_inbox/_conflicts/` and notifies.
5. Audit log enables full reconstruction if any of the above fails.

### Runtime model

- **Python 3.12+**, single language stack.
- One long-running Python process for Telegram polling.
- Cron jobs (launchd plists on a Mac mini) for daily digest (8am), weekly digest (Sunday 6pm), reminder dispatcher (every 5 min), conflict watcher (every 1 min).
- Single-instance enforcement via `flock` on a `/tmp/personal-assistant.lock`.
- Secrets in launchd `EnvironmentVariables` (Telegram token); `ANTHROPIC_API_KEY` inherited from `claude -p`'s default keychain.

### Classifier + safety net

- LLM-based classifier reads YAML at startup; new domains register by adding YAML, no code change.
- Ambiguous classifications fall back to `vault/_inbox/` — the safety net prevents silent data loss.
- Weekly digest includes an "inbox triage" step that surfaces pending entries to the user for confirmation/relabeling.

### INDEX refresh policy

`INDEX.md` regenerates inline after every 5 writes (blocks ~2-4 seconds).
Flat policy. The freshly-rebuilt INDEX becomes the next turn's vocabulary
seed for keyword expansion — closing the loop between accumulation and
retrievability.

### Deep modules

The kernel separates into a small set of deep modules — simple interfaces,
encapsulated complexity, individually testable:

- **vault** — `atomic_write`, mtime guard, append helper, glob primitive.
- **audit** — append-only JSONL writer with sha256 ids and required-field validation.
- **retrieval** — `gather_context(query, config, budget)` orchestrates the tiered tool palette, INDEX-first read, keyword expansion, backlink walk; the eight Booleans live here as configuration.
- **claude_runner** — subprocess wrapper for `claude -p`; parses `usage` for token telemetry.
- **classifier** — reads domain YAMLs, calls runner with classifier prompt, returns intent.
- **session** — `load_or_create` / `update` for the active-session summary.
- **index** — INDEX.md regenerator (pure function over vault state).
- **conflict_watcher** — glob, structural diff, optional LLM merge, stage-to-inbox.
- **proactive** — task entrypoint (`--task daily-digest|weekly-digest|check-reminders`); composes from domain digest modules.
- **telegram_bridge** — polling loop, dispatches to orchestrator.
- **orchestrator** — wires the per-turn flow.

Each domain plugin contributes its own deep module: `handler.write` /
`handler.read` (idempotent on sha256 id) plus an optional `digest.summarize`.

---

## Testing Decisions

### What makes a good test

Tests verify **external behavior**, not implementation details. For
example, `atomic_write` is tested via "writing to a path produces a
complete file even if the process is killed mid-write" — not "calls
`os.replace` after writing to a temp file." This protects the test suite
from breaking when the implementation changes within a stable interface.

### Modules to test

| Module | Tests |
|---|---|
| **vault** | atomic write produces a complete file under simulated mid-write failure; mtime guard refuses writes within 30-min buffer; append helper is duplicate-safe by sha256 id; glob returns sorted, deterministic results |
| **audit** | every required field present; entries are append-only and durable; daily rotation lands in correct file; sha256 ids are stable for identical inputs |
| **retrieval** | respects token budget within configured headroom; tier order honored (index → session → grep); INDEX-first when `vault_index_first=true`; `expand_keywords` adds synonyms when configured; backlink walk caps at `backlink_max_hops` |
| **claude_runner** | parses `usage` correctly into `tokens_in` / `tokens_out`; surfaces non-zero exit codes as errors; handles transient failures via `tenacity` retry |
| **classifier** | reads all `domains/*/domain.yaml` at startup; classifying a clear-intent message routes correctly; ambiguous-confidence message falls back to `_inbox` |
| **conflict_watcher** | detects a synthetic `* (Conflict *).md`; LLM-merges when `conflict_auto_merge=true`; stages and notifies otherwise; never overwrites the user's copy |
| **per-domain handler.write** | re-running the same input produces the same id and no duplicate row (idempotency); writes only into the plugin's own path |
| **per-domain handler.read** | structured queries return exact numeric answers (no LLM hand-arithmetic); shape matches the documented signatures |

### Eval (head-to-head, beyond unit tests)

The eval is the *quality* test, distinct from unit tests of behavior. It
runs both `configs/default.yaml` (engineered) and `configs/baseline.yaml`
(vanilla) over the same input set and scores the outputs.

- **Score dimensions:** 5-point Likert on accuracy, grounding, conciseness, connection, trust. v1 is manual scoring; v2 adds LLM-as-judge.
- **Token budget axis:** parsed from audit logs; same budget for both configs (fairness).
- **Case set:** ~10 synthetic + ~5 of the user's own seed queries (in `eval/seed_queries/00-jason-seed-queries.md`) for v1; grows over time as cases are harvested from real interactions.
- **Per-domain cases:** each plugin ships its own `eval/cases.jsonl`. A plugin without eval cases is not promoted past `_inbox` triage — "no eval, no promotion" is a hard gate.

### Prior art

The eval methodology — head-to-head config sweeps, per-feature ablation
boolean toggles, structured judge dimensions, token-budget tracking — is
adapted from the user's existing NEXUS eval suite for the
LangGraph-based agentic-rag-api project (see
`stream4-fuelix/plans/nexus-eval-suite-prd-20260427.md`). The adaptation
swaps a vector-store retrieval target for a local file-based vault but
preserves the head-to-head structure.

---

## Out of Scope

These are **not** part of v1, by design:

- **Voice notes** — text-only for v1; voice deferred until the text path is solid.
- **Multi-user** — single-user assumption baked into the architecture (flock, single-process runtime, cron model).
- **Real-time webhook for Telegram** — polling is sufficient at personal scale.
- **Web UI** — Telegram + Obsidian *are* the interface.
- **Embedding-based retrieval fallback** — only added if the eval ever shows a gap that pure agentic retrieval can't close. Documented as an escape hatch in `kernel/RETRIEVAL.md`.
- **LLM-as-judge for eval scoring v1** — manual scoring on a small case set is sufficient for the v1 portfolio writeup; LLM-judge automation is v2.
- **Cross-account / shared vault** — single-machine architecture; multi-machine sync is an explicit non-goal.
- **Auto-rotation / archival of audit logs** — daily files are kept indefinitely on Drive for v1.
- **Mobile-side vault editing safety** beyond the 30-minute buffer — if the user explicitly wants to edit a file while the agent is mid-turn, that's an acceptable race for v1.

---

## Further Notes

### The "learn over time" mechanism

This is the part most likely to be misunderstood. The system does **not**
fine-tune, embed, or cache any model state. The personalization mechanism
is entirely *in-context*: the vault accumulates structured signal over
time, and every plan or query request reads recent history fresh on each
turn. The agent's response adapts to current state because the prompt it
sees on each turn includes current state. Closing the loop: future plans
read past plans + actuals via `query_fitness(kind='plans', ...)` and
`query_fitness(kind='compliance', ...)`.

This is the portfolio's most defensible technical claim: *adaptive
personalization without fine-tuning is achievable when the vault is
well-shaped and the agent is told to read it carefully on every turn.*

### Eval-driven course corrections

The eval is the engineering driver, not just validation. If the engineered
config underperforms baseline on any axis, that triggers refinement of
the engineering — not a relaxation of the eval. The two named triggers
recorded during grilling:

- If pure-agentic retrieval underperforms a hypothetical embedding fallback on recall, the embedding fallback documented in `kernel/RETRIEVAL.md` becomes a candidate for v2.
- If the conflict-watcher LLM-merge produces silent corruption on real conflicts, `conflict_auto_merge` is downgraded to `false` in default and stays as a stage-only operation until the merge prompt is improved.

### Idempotency-by-content-hash as a load-bearing invariant

Every append uses a sha256 of the content as its id. This single decision
solves: Drive sync replays, classifier reruns, double-logged events,
mid-failure retries, and statement re-uploads — without any deduplication
logic in any reader. Readers can naively `cat` the JSONL and call it done.
Future-you will be grateful for this invariant.

### Anonymization & privacy

No personal data is committed to git. `.gitignore` excludes `vault/**`
except `vault/_index/INDEX.md` (the sample). `domains/fitness/profile.template.yaml`
is the schema; the user's actual `vault/fitness/profile.yaml` is gitignored.
Audit logs are local-only by design.

### CLAUDE.md as architectural enforcer

The project-level `CLAUDE.md` is load-bearing for keeping future Claude
Code sessions from drifting the architecture. It declares the cardinal
rule, lists the off-limits kernel files, walks through a worked plugin
example ("add fitness tracking"), and enumerates the five concurrency
invariants. It is intended to be auto-loaded on every Claude Code
invocation in this repo and treated as binding.

### Implementation sequencing

`IMPLEMENTATION.md` lays out a 10-day build plan in dependency order, with
**every milestone as a working slice** rather than a half-built component.
Day 1-2 is a vertical slice (Telegram → classified → journaled → indexed
→ audited → reply); each subsequent day adds one domain or capability.
Day 7 is the eval harness, deliberately last because it depends on the
audit-log shape being stable. Day 8 is real-use shakedown; Day 9
iteration; Day 10 the portfolio writeup.

A new fitness build slot will need to be inserted (likely as Day 4.5 or
extending Day 4) since fitness was added as a fifth plugin during the
final round of grilling.
