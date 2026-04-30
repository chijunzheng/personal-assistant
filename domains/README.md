# Domain Plugins

Each subdirectory is a self-contained domain plugin. The kernel discovers them
at startup and routes Telegram turns based on classifier output.

## Adding a new domain

1. `mkdir domains/<name>/`
2. Drop in the four files below
3. Restart the runner — the classifier picks up the new intents automatically

```
domains/<name>/
├── domain.yaml      # the contract — declarative spec
├── prompt.md        # LLM-facing instructions for handling this domain
├── handler.py       # storage I/O, validation, extraction logic
├── digest.py        # OPTIONAL: contributes to daily/weekly digest
└── eval/
    └── cases.jsonl  # eval cases for this domain
```

## The contract — `domain.yaml`

```yaml
name: fitness                    # unique
description: "Workout logs, body metrics, training plans"
intents:                         # classifier outputs that route here
  - fitness.workout
  - fitness.metric
  - fitness.query
storage:
  type: markdown | table | state | jsonl
  path: vault/fitness/
  schema: ...                    # type-specific
handlers:
  classify: handler.py:classify  # optional override; default uses LLM
  write:    handler.py:write     # called on persist intents
  read:     handler.py:read      # called on query intents
digest:
  enabled: true
  cadence: daily | weekly
  module: digest.py:summarize
eval:
  cases: eval/cases.jsonl
```

## Rules of the seam

1. **Plugins never import each other.** Cross-domain queries route through the
   kernel's vault index, not direct calls.
2. **Plugins never write the audit log.** The kernel does it after `write:`
   returns. This keeps the log domain-agnostic.
3. **Plugins must be idempotent on `write:`** with a unique key (e.g., a
   transaction hash for finance) so reruns don't double-write.
4. **Plugins ship their own eval cases.** A plugin without `eval/cases.jsonl`
   is not promoted past `_inbox/` triage status.

## Currently registered domains

- `journal/` — substantive thoughts, learnings, ideas (markdown narrative)
- `finance/` — transactions and spending queries (TBD storage shape, see Q5)
- `inventory/` — household items and state (state file)
- `reminder/` — scheduled and state-derived reminders (kernel cron contract)
- `fitness/` — workouts, meals, body metrics, adaptive plans (hybrid storage; cross-domain reads from journal/finance/inventory)

## Examples of future domains (not yet built)

- `reading/` — books, articles, highlights
- `recipes/` — saved recipes, meal plans, grocery generation
- `contacts/` — light CRM, last-talked-to dates, follow-ups
- `travel/` — trip plans, packing lists, post-trip notes
- `projects/` — work/side-project state, blocked items, next actions
