# Implementation Plan — Week 1 to Portfolio v1

Eight engineering decisions, three core domains, one eval harness, one
proactive layer. Built in dependency order so every milestone is a
*working slice* — never a half-built component blocking another.

## The build sequence

### Day 1–2 — Kernel skeleton + journal plugin (vertical slice)

Goal: end-to-end "Telegram message → classified → journaled to vault → INDEX
refreshed → audit logged → reply sent." One domain only (journal). No
finance, no inventory, no proactive layer, no eval yet.

Build order:
1. `kernel/vault.py` — atomic_write, mtime guard, append helper, glob
2. `kernel/audit.py` — append-only JSONL writer with sha256 IDs
3. `kernel/claude_runner.py` — subprocess wrapper for `claude -p`, parses
   usage tokens
4. `kernel/classifier.py` — reads `domains/*/domain.yaml`, calls
   claude_runner with classifier prompt, returns intent
5. `kernel/session.py` — load/create/update active_session.md
6. `kernel/retrieval.py` — minimal version: read INDEX → read session →
   grep+read. Per-domain shaping wiring stubbed.
7. `kernel/index.py` — INDEX.md generator (every 5 writes, inline trigger)
8. `kernel/orchestrator.py` — wires steps 1–7 into a per-turn handler
9. `kernel/telegram_bridge.py` — polling loop, calls orchestrator
10. `domains/journal/handler.py` — write (create new file), read (return
    grep matches)
11. End-to-end smoke test: send a message, verify file appears, verify
    audit line, verify INDEX refreshes after 5 writes

**Done when:** you can text the bot from your phone, get a reasonable reply,
and find the journaled note in `vault/journal/` with a matching audit line.

### Day 3 — Finance plugin

Goal: upload a credit card PDF → transactions extracted to JSONL →
"how much did I spend on coffee last month?" returns a real `SUM`.

1. `domains/finance/handler.py:write` — PDF/image → claude -p extracts
   transactions → idempotent append to `transactions.jsonl`
2. `domains/finance/handler.py:read` — `query_finance(category, date_range,
   agg)` tool. Real Python aggregation, no LLM-summing.
3. Wire `query_finance` into the agent's tool palette in
   `kernel/retrieval.py` (only when `per_domain_shaping: true`)
4. Test with a real (anonymized) statement

**Done when:** uploading a statement and asking "coffee spend last month"
returns a number that matches manual addition.

### Day 4 — Inventory plugin

Goal: "bought 2 milks" updates state.yaml; "what's running low?" returns the
right list.

1. `domains/inventory/handler.py:write` — appends event, recomputes
   `state.yaml` from event log
2. `domains/inventory/handler.py:read` — `query_inventory(item|low_stock|
   list)`
3. Wire the query tool similarly
4. Test add/consume/query flow

**Done when:** add/consume/query round-trip works correctly across multiple
turns.

### Day 5 — Reminder plugin + proactive scaffolding

Goal: scheduled and state-derived reminders fire reliably; daily digest
composes from domain plugins.

1. `domains/reminder/handler.py` — write (parse natural language → reminder
   row), read (list pending)
2. `kernel/proactive.py` — daily-digest task, weekly-digest task,
   check-reminders task
3. `domains/journal/digest.py` — weekly slice
4. `domains/finance/digest.py` — weekly slice
5. `domains/inventory/digest.py` — daily slice (low-stock)
6. launchd plists in `infra/launchd/` for digest schedules
7. Manual test: `python -m kernel.proactive --task daily-digest` produces
   a reasonable Telegram message

**Done when:** "remind me when I run out of AAA" fires correctly when
inventory consumes the last AAA; daily digest arrives at 8am.

### Day 6 — Conflict watcher + sync hardening

Goal: Drive sync conflicts get detected, merged-or-staged, and audited.

1. `kernel/conflict_watcher.py` — glob, structural diff, LLM merge gated on
   config flag, stage to `_inbox/_conflicts/`, telegram notify
2. launchd plist for the watcher (every 1 min)
3. Manual test: induce a conflict file, verify the watcher handles it

**Done when:** synthetic conflict file disappears (merged or staged) within
~1 min, audit log records the resolution.

### Day 7 — Eval harness

Goal: head-to-head `default.yaml` vs `baseline.yaml` on a small case set.

1. `eval/cases/` — assemble ~10 synthetic + ~5 of jason's seed queries from
   `eval/seed_queries/00-jason-seed-queries.md`
2. `eval/run.py` — for each case: run engineered, run baseline, capture
   audit logs (tokens), capture replies
3. `eval/score.py` — 5-dim Likert scoring (manual for v1; LLM-as-judge for
   v2). Token chart from audit aggregation.
4. `eval/report.py` — markdown table comparing the two, written to
   `docs/eval-progression.md`

**Done when:** running `python -m eval.run` produces a report with two
scored runs and a token-budget chart.

### Day 8 — Real-use shakedown

No new code. Use the system as designed via Telegram. Drop scattered
thoughts. Upload a real (or anonymized) statement. Track inventory. Confirm:

- Journals get filed correctly
- INDEX.md stays fresh
- Daily digest fires and is useful
- A reminder fires when expected
- Audit log has no gaps

Anything broken → file as a domain `eval/cases.jsonl` entry, fix in Day 9.

### Day 9 — Iteration based on shakedown

Fix the issues from Day 8. Add eval cases for any failure modes discovered.
Re-run head-to-head; record numbers.

### Day 10 — Portfolio writeup

`docs/writeup.md`:
- The thesis: "context engineering achieves equal-or-better quality at lower
  token budget than vanilla agentic baseline"
- Architecture diagram (kernel + 4 plugins + proactive layer)
- The eight engineering decisions, each as a config Boolean
- Eval results: 5-dim chart, token chart
- Sync resilience: example conflict from real use, audit-log excerpt
- "How to add a new use case" — the plugin recipe

## Critical-path dependencies

```
vault.py ──┬─→ audit.py ──┬─→ orchestrator.py ──→ telegram_bridge.py
           │              │
           └─→ index.py ──┘
                          
claude_runner.py ──→ classifier.py ──→ orchestrator.py
session.py ────────────────────────────→ orchestrator.py
retrieval.py ──────────────────────────→ orchestrator.py

domains/journal/ ─────→ orchestrator.py (registers via YAML)
domains/finance/ ─────→ retrieval.py (tool registration)
domains/inventory/ ───→ retrieval.py (tool registration)
domains/reminder/ ────→ proactive.py
```

Build kernel files in the column-1 order, then domains, then proactive.
Eval harness comes last — deliberately — because it depends on the
audit-log shape being stable.

## What's out of scope for v1

- Voice notes (deferred)
- Multi-user (deferred indefinitely)
- LLM-as-judge for eval scoring (manual scoring v1; automate v2)
- Embedding fallback tool (only if eval shows a gap)
- Web UI (Telegram + Obsidian is the interface)
- Real-time webhook for Telegram (polling is fine)

## Definition of "done" for portfolio v1

- 10 days of real personal use across 3+ domains
- Eval results in a chart in `docs/writeup.md`
- ≥30 audit log entries demonstrating real-world resilience
- One documented conflict resolution from real Drive usage
- README, CLAUDE.md, and per-component docs all current
