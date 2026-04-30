# Personal Assistant — Kernel + Domain Plugins

A second-brain agent built on `claude -p` headless. Telegram for input, Obsidian
for visualization, Google Drive as the sync layer.

## Architecture

Two layers, separated at a hard seam:

```
+--------------------------------------------------------------+
|                          KERNEL                              |
|  Telegram bridge • claude -p runner • classifier • retrieval |
|  session manager • audit log • vault I/O • eval harness      |
+--------------------------------------------------------------+
                              |
              registers / dispatches to / evaluates
                              |
+--------------------------------------------------------------+
|                       DOMAIN PLUGINS                         |
|        journal/    finance/    inventory/    <future>/       |
+--------------------------------------------------------------+
```

The kernel **never changes** when adding a new use case. New use case =
new directory under `domains/` + a `domain.yaml`. The classifier auto-discovers
intents from the registry; the eval harness auto-discovers per-domain cases.

## Directory map

| Path | Purpose |
|---|---|
| `kernel/` | The unchanging core. Code that orchestrates every turn. |
| `kernel/prompts/` | System prompts and classifier prompts. |
| `domains/<name>/` | One plugin per use case. See `domains/README.md`. |
| `vault/` | The Obsidian vault — the single source of truth for memory. |
| `vault/_inbox/` | Safety net: ambiguous classifications land here for weekly triage. |
| `vault/_audit/` | Append-only JSONL audit log. One file per day. |
| `vault/_index/` | Auto-maintained index files (vault TOC, recent activity, tag map). |
| `eval/` | Head-to-head eval harness (engineered vs. baseline). |
| `eval/cases/` | Aggregated eval cases harvested from `domains/*/eval/`. |
| `eval/results/` | Versioned eval runs. |
| `configs/` | Which engineering decisions are ON for a given run. |

## The portfolio thesis

> *Meticulous context engineering produces equal-or-better answers using less
> context than a vanilla agentic baseline — with no embeddings, no vector DB,
> just filesystem tools and structural conventions.*

Measured by head-to-head eval on ~20–30 real interactions, scored on five
dimensions (accuracy, grounding, conciseness, connection, trust) with a
secondary axis on token budget per turn.

## The eight engineering decisions (the eval matrix)

Each is a Boolean toggle in `configs/default.yaml` (all ON) vs
`configs/baseline.yaml` (all OFF). The portfolio claim is that flipping
these eight bits ON yields measurable quality gains at fixed token budget.

1. `tiered_retrieval` — ordered tool palette (index → session → targeted reads)
2. `per_domain_shaping` — structured query tools per domain (`query_finance`, `query_inventory`)
3. `recency_weighting` — prefer recent files; filename dates make this free
4. `active_session_summary` — compact running session summary across turns
5. `vault_index_first` — INDEX.md as vocabulary seed for keyword expansion
6. `backlink_expansion` — 1-hop graph walk from retrieved notes
7. `suggested_actions` — LLM pass over digests adds advice, not just data
8. `conflict_auto_merge` — LLM-merge of Drive sync conflicts vs stage-only

## Documentation map

| Reading order | File | What it covers |
|---|---|---|
| 1 | `CLAUDE.md` | Project instructions for Claude Code — kernel/plugin discipline, invariants |
| 2 | `kernel/RUNTIME.md` | Language, package layout, process model, telemetry |
| 3 | `kernel/RETRIEVAL.md` | Pure agentic retrieval architecture (no embeddings) |
| 4 | `kernel/SYNC.md` | Drive concurrency strategy — five layered defenses |
| 5 | `kernel/PROACTIVE.md` | Digests, reminders, suggested-actions LLM pass |
| 6 | `domains/README.md` | Plugin contract — how to add a new use case |
| 7 | [GitHub issues](https://github.com/chijunzheng/personal-assistant/issues) | Execution queue — pick the next issue whose blockers are all closed |
| 8 | `configs/default.yaml`, `configs/baseline.yaml` | The eval matrix encoded as YAML |

## Status

**Design phase complete.** Twelve foundational decisions locked, all artifacts
in place. Implementation queue lives at
[GitHub issues](https://github.com/chijunzheng/personal-assistant/issues) —
17 vertical-slice issues in dependency order. The first issue (#1, Telegram
echo tracer) builds `kernel/vault.py`'s `atomic_write` plus the Telegram
+ `claude -p` integration as one end-to-end smoke test.

No code yet — design and configs only.
