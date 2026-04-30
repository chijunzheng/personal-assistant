# Personal Assistant — Project Instructions for Claude Code

You are working inside Jason's personal-assistant codebase: a `claude -p`-driven
second-brain agent with Telegram input, Obsidian/Drive vault as memory.

These instructions are **load-bearing**. Read them fully before suggesting edits.

---

## The cardinal rule

> **KERNEL NEVER CHANGES TO ADD A USE CASE. NEW USE CASES ARE PLUGINS.**

The system has two layers separated at a hard architectural seam:

```
+----------------------- KERNEL (do not modify when adding features) -------+
|  kernel/orchestrator.py · kernel/classifier.py · kernel/retrieval.py      |
|  kernel/session.py · kernel/audit.py · kernel/vault.py · kernel/index.py  |
|  kernel/prompts/* · configs/default.yaml · configs/baseline.yaml          |
+---------------------------------------------------------------------------+
                                    |
                          dispatches / evaluates
                                    |
+----------------------- DOMAIN PLUGINS (modify these) ---------------------+
|  domains/journal/ · domains/finance/ · domains/inventory/ · <future>/     |
+---------------------------------------------------------------------------+
```

If a request *feels* like it requires changing the kernel to add a feature,
**stop and re-read this file.** The answer is almost always "make a plugin."

---

## When asked to "add X" (where X is fitness, recipes, contacts, reading list, etc.)

### DO

1. `mkdir domains/<name>/` and `domains/<name>/eval/`
2. Create `domains/<name>/domain.yaml` declaring intents, storage, handlers
3. Create `domains/<name>/prompt.md` — LLM-facing instructions for the domain
4. Create `domains/<name>/handler.py` — write/read functions, idempotent on write
5. Optionally create `domains/<name>/digest.py` if the domain contributes to digests
6. Create `domains/<name>/eval/cases.jsonl` — at least 5 cases before promoting
   from `_inbox/` triage. **No eval, no promotion.**
7. Run eval against both `configs/default.yaml` and `configs/baseline.yaml` to
   confirm the new domain doesn't break either path.
8. Update `domains/README.md`'s "Currently registered domains" section.

### DON'T

- Edit `kernel/classifier.py` to add an `if intent == 'fitness'` branch.
  The classifier is data-driven — it auto-discovers intents from
  `domains/*/domain.yaml`. **Adding YAML is the entire registration step.**
- Edit `kernel/orchestrator.py` to handle a new domain's flow.
- Edit `configs/default.yaml` or `configs/baseline.yaml` *unless* you are
  adding a new context-engineering decision (in which case ask Jason first
  and update both configs symmetrically).
- Reach into another domain's directory. Plugins never import each other.
  Cross-domain queries route through `kernel/vault.py` indexes.
- Write to `vault/_audit/` from a plugin handler. The kernel writes audit
  entries after `handler.py:write` returns. Plugins must be log-silent.

---

## Files that must never be edited as part of adding a feature

| Path | Why off-limits |
|---|---|
| `kernel/orchestrator.py` | Per-turn dispatch; changing breaks all domains |
| `kernel/classifier.py` | Data-driven; new domains register via YAML, not code |
| `kernel/retrieval.py` | Implements the six engineering decisions; eval baseline |
| `kernel/session.py` | Session model is uniform across domains |
| `kernel/audit.py` | Domain-agnostic audit invariant |
| `kernel/vault.py` | Vault I/O primitives; concurrent-safe |
| `kernel/index.py` | INDEX.md refresh job; runs every 5 writes |
| `kernel/prompts/system.md` | Affects all turns; eval-relevant |
| `kernel/prompts/classifier.md` | Affects all routing; eval-relevant |
| `configs/default.yaml` | Engineered config — eval ground truth |
| `configs/baseline.yaml` | Baseline config — eval ground truth |

If the request genuinely requires touching one of these, **surface it
explicitly to Jason** with the reasoning and the alternative (plugin) you
considered. Do not silently edit kernel files.

---

## Worked example: "add fitness tracking"

The user says: *"add fitness tracking — I want to log workouts and ask
'how often did I run last month?'"*

### Wrong response (what an unguided agent does)

> *Edits `kernel/classifier.py` to add `'fitness.workout'` to the intent
> enum. Edits `kernel/orchestrator.py` to add an `if intent.startswith('fitness'):`
> branch. Adds 200 lines of fitness logic to the kernel.*

This breaks the architecture. The classifier is now coupled to fitness.
Adding the next domain (recipes) will repeat the pattern. The kernel grows
linearly with features. This is what we are explicitly preventing.

### Right response

```
1. mkdir -p domains/fitness/eval/

2. Write domains/fitness/domain.yaml:

     name: fitness
     description: "Workout logs and training metrics"
     intents:
       - fitness.workout      # log a session
       - fitness.metric       # log a measurement (weight, HR, etc.)
       - fitness.query        # ask about training history
     storage:
       type: jsonl            # workouts as structured rows, like finance
       path: vault/fitness/
       schema:
         workouts: vault/fitness/workouts.jsonl
         metrics:  vault/fitness/metrics.jsonl
     handlers:
       write: handler.py:write
       read:  handler.py:read
     digest:
       enabled: true
       cadence: weekly
       module: digest.py:summarize
     eval:
       cases: eval/cases.jsonl

3. Write domains/fitness/prompt.md — how the agent should extract
   workout fields (date, type, duration, distance, intensity, notes)
   from natural language.

4. Write domains/fitness/handler.py implementing:
   - write(intent, parsed) → appends to workouts.jsonl OR metrics.jsonl,
     returns the appended row's id
   - read(intent, query) → for fitness.query, runs structured aggregation
     over the JSONL files (e.g., count runs in last 30 days)
   Both must be idempotent — writing the same row twice yields the same id.

5. Optionally write domains/fitness/digest.py — what shows up in the
   weekly digest from this domain.

6. Write domains/fitness/eval/cases.jsonl — at least 5 head-to-head cases:
   workout-extraction accuracy, query correctness, edge cases.

7. Run: ./eval/run.py --domain fitness --config default
        ./eval/run.py --domain fitness --config baseline
   Confirm engineered config beats baseline (or document why not).

8. Update domains/README.md → add fitness to the registered list.

9. Commit. Done. Kernel was not touched.
```

The classifier picks up `fitness.workout` automatically from the YAML.
The orchestrator dispatches automatically via the registered handler.
The eval harness automatically discovers the new cases.

This is the recipe. **Every new use case follows it.**

---

## When kernel changes ARE appropriate (the escape hatch)

The plugin model handles 95% of feature additions. The 5% that legitimately
needs kernel work:

| Situation | Why it's a kernel change |
|---|---|
| New input modality (image, voice) | Telegram bridge + classifier are kernel-level |
| New context-engineering decision | configs/* schema + retrieval.py change |
| Cross-domain primitive (e.g., "any domain can request a reminder") | New kernel API; symmetric for all plugins |
| Performance fix to retrieval | Retrieval is shared infrastructure |
| Audit log schema migration | Audit format is a kernel invariant |

For these, **propose the change first** with reasoning before editing.
Document any kernel change in `docs/decisions/<date>-<slug>.md` (an ADR).

---

## Quality gates (every change, kernel or plugin)

1. **Eval must run clean.** `./eval/run.py` against both configs, no
   regressions on existing domains.
2. **Audit log must continue to populate.** Spot-check `vault/_audit/`
   has new entries with the right shape after a smoke test.
3. **No direct vault writes outside `kernel/vault.py`.** All `.md` and
   `.jsonl` writes route through the kernel API so audit and indexing
   trigger correctly.
4. **Plugins must be idempotent on write.** Use content-hash IDs (finance
   does `sha256` of the statement line). Re-running must not duplicate.
5. **Coding style** (from `~/.claude/rules/coding-style.md`):
   immutability, small files (<400 lines), comprehensive error handling,
   no mutation, no console.log, no hardcoded secrets.

---

## Common pitfalls (do not do these)

1. **Adding an `if` branch in `classifier.py` for a new domain** — the
   classifier reads YAML; add YAML, not code.
2. **Plugin imports another plugin** — never. Route via kernel.
3. **Plugin writes audit log directly** — kernel does it after handler returns.
4. **Plugin keeps in-memory state across turns** — `claude -p` is one-shot;
   state lives in vault files only.
5. **Eval added without a baseline run** — eval cases are useless without
   the head-to-head against `baseline.yaml`. Always run both.
6. **Editing `configs/baseline.yaml` to "make the test pass"** — the baseline
   is *frozen* by design. If the eval fails, fix the engineering, not the
   baseline.
7. **Writing markdown for structured data** — finance and inventory live in
   `.jsonl` / `.yaml`, *not* prose markdown. Markdown is for narrative
   (journal). Mixing these breaks query correctness.

---

## Architectural artifacts to consult

| Question | File |
|---|---|
| What's the overall architecture? | `README.md` |
| How does retrieval work without embeddings? | `kernel/RETRIEVAL.md` |
| How are Drive sync conflicts handled? | `kernel/SYNC.md` |
| How does the proactive layer work? | `kernel/PROACTIVE.md` |
| What's the plugin contract? | `domains/README.md` |
| What's the engineered system claiming to do? | `configs/default.yaml` |
| What's the baseline that must be beaten? | `configs/baseline.yaml` |
| What does INDEX.md look like? | `vault/_index/INDEX.md` |
| What's a worked plugin example? | `domains/journal/domain.yaml` |

## Concurrency invariants (non-negotiable)

When writing kernel code that touches the vault, these rules are MANDATORY.
See `kernel/SYNC.md` for full reasoning.

1. **Atomic writes only.** Every vault write goes through
   `kernel/vault.py:atomic_write` (tmp + os.replace). Never `open(path, 'w')`
   directly on a vault file.
2. **Event logs are append-only with sha256 IDs.** Never edit or rewrite a
   `.jsonl` event log. Readers dedupe by `id`.
3. **Respect the 30-min user-edit buffer.** Before modifying any narrative
   `.md` file, check mtime; if within buffer, fall back to create-new or
   stage-to-pending.
4. **Audit before write.** No vault write without an audit-log entry.
5. **Single agent assumption.** Kernel runs on exactly one machine. Don't
   add code that assumes otherwise.

---

## How to ask Jason for clarification

If a request is ambiguous about whether it's a plugin addition or a kernel
change, ask explicitly:

> *"This looks like it needs a new domain plugin under `domains/<X>/`. I'd
> create the four standard files there and not touch the kernel. Confirm?"*

Get confirmation before editing anything in `kernel/` or `configs/`.

---

## TL;DR

> **Add files, don't change files.** New use cases are new directories
> under `domains/`, not new branches in kernel code. The classifier and
> orchestrator are data-driven — they read YAML at startup. If you find
> yourself editing the kernel to add a feature, you are doing it wrong.
> Stop, re-read this file, and reach for the plugin recipe instead.
