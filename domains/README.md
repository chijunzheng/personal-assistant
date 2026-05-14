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
view:                            # REQUIRED if storage is not pure markdown
  module: view.py:render         # plugin-owned pure renderer
digest:
  enabled: true
  cadence: daily | weekly
  module: digest.py:summarize
eval:
  cases: eval/cases.jsonl
```

### The `view:` block (ADR 2026-05-14)

Any domain whose `storage.type` is not pure `markdown` — i.e. `jsonl`,
`table`, `state`, or `hybrid` — **must** declare a `view:` block. It points
at a plugin-owned pure renderer:

```python
def render(vault_root: Path, *, now: datetime) -> None: ...
```

The kernel calls this renderer after every `write:`, right next to the
INDEX.md refresh. The renderer reads the canonical `.jsonl`/`.yaml`,
regenerates *all* its output `.md` files, and writes them through
`kernel.vault.atomic_write`. Each generated file carries a `generated_by:`
frontmatter marker so the kernel keeps it out of every read path — its
`[[wikilinks]]` feed the backlink graph, but it is never queried or
retrieved as a source note. The canonical structured files stay canonical;
the markdown is a one-way, Obsidian-viewable projection (see `CLAUDE.md`
pitfall #7's carve-out). Render is best-effort: a render failure is
audit-logged but never rolls back the canonical write.

Pure-markdown domains (`journal/`) are already Obsidian-native and declare
no `view:`.

## Rules of the seam

1. **Plugins never import each other.** Cross-domain queries route through the
   kernel's vault index, not direct calls.
2. **Plugins never write the audit log.** The kernel does it after `write:`
   returns. This keeps the log domain-agnostic.
3. **Plugins must be idempotent on `write:`** with a unique key (e.g., a
   transaction hash for finance) so reruns don't double-write.
4. **Plugins ship their own eval cases.** A plugin without `eval/cases.jsonl`
   is not promoted past `_inbox/` triage status. *No eval, no promotion.*
5. **Structured domains ship a markdown view.** A domain whose storage is not
   pure markdown must declare a `view:` block (see above). A structured
   plugin without one is not promoted past `_inbox/` triage status, the same
   way one missing `eval/` is not. *No view, no promotion.*

## Currently registered domains

Storage column notes whether the domain ships a generated markdown `view:`
(ADR 2026-05-14). Pure-markdown domains are exempt; structured domains
without one yet are mid-rollout.

- `journal/` — substantive thoughts, learnings, ideas (markdown narrative; no `view:` — already Obsidian-native)
- `finance/` — transactions and spending queries (jsonl storage; **ships a `view:`** — monthly rollup `.md` files)
- `inventory/` — household items and state (hybrid storage; `view:` pending rollout)
- `reminder/` — scheduled and state-derived reminders (jsonl storage, kernel cron contract; projects `events.jsonl` → `reminders.md`, `view:` retrofit to the kernel primitive pending)
- `fitness/` — workouts, meals, body metrics, adaptive plans (hybrid storage; cross-domain reads from journal/finance/inventory; `view:` pending rollout)

## Examples of future domains (not yet built)

- `reading/` — books, articles, highlights
- `recipes/` — saved recipes, meal plans, grocery generation
- `contacts/` — light CRM, last-talked-to dates, follow-ups
- `travel/` — trip plans, packing lists, post-trip notes
- `projects/` — work/side-project state, blocked items, next actions
