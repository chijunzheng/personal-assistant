# ADR 2026-05-14: Kernel primitive for Obsidian-viewable markdown views

**Status:** Accepted
**Date:** 2026-05-14

## Context

The vault is Jason's second brain, read primarily through Obsidian. But
half the vault is invisible there. `journal/` is native markdown — it
renders, links, and shows up in Obsidian's graph. The structured domains
do not: `finance/transactions.jsonl`, `inventory/state.yaml`,
`fitness/{workouts,meals,metrics}.jsonl` are canonical data formats that
Obsidian cannot display. The user can ask the bot "how much did I spend
on groceries?" over Telegram, but cannot *browse* their own finance data
in the tool they use for everything else.

Issue #32 (commit a9f9317) already solved this for one domain. The
`reminder/` plugin gained `view.py` — a one-way projection from
`events.jsonl` to a human-readable `reminders.md`. The JSONL stays
canonical (append-only, sha256 ids); the markdown is regenerated on every
write and never read back. It works. But it is **plugin-local**: the
handler calls `_regenerate_view()` itself, the trigger is hand-wired, and
nothing about the architecture says the next domain has to do the same.

Two problems with leaving it there:

**1. The projection gap will keep reopening.** Every new structured
domain (`recipes/`, `reading/`, ...) starts life invisible in Obsidian.
"Add a view" becomes a thing someone has to remember, every time. A
convention nobody enforces is a convention that erodes.

**2. There is no wiki.** `kernel/index.py` already parses `[[wikilinks]]`,
builds a tag map, and detects orphans and backlinks — the machinery for a
linked vault exists. But the structured domains produce no markdown, so
they produce no links, so they are absent from the graph entirely. The
journal is a loose pile of notes, not a wiki, because nothing connects the
notes.

The user's ask, distilled: *every structured domain should be viewable in
Obsidian, and the vault should read like an interlinked wiki, not a pile
of disconnected files.*

There is a real tension to resolve. `CLAUDE.md` pitfall #7 says: *"Writing
markdown for structured data — finance and inventory live in `.jsonl` /
`.yaml`, not prose markdown. Mixing these breaks query correctness."* This
ADR does not contradict that rule — it sharpens it. The rule is about
**what you query against**, not about file extensions. Canonical data
stays `.jsonl`/`.yaml`; `query_finance()` and friends keep aggregating
over the structured files exactly as today. The markdown is a *generated,
never-read-back projection* — the same category as `INDEX.md` or a
digest, both of which are markdown derived from structured sources. What
makes the distinction safe is that it is now **machine-checkable**: every
generated file carries a `generated_by:` frontmatter marker, and the
kernel uses that marker to keep generated files out of every read path.

## Decision

Introduce a kernel primitive: **structured-storage domains declare a
markdown view; the kernel renders it after every write.** Five coordinated
changes, scoped so none becomes a per-domain branch.

**1. `domain.yaml` gains a `view:` block.**

```yaml
view:
  module: view.py:render        # plugin-owned pure renderer
```

The block is **required for any domain whose `storage.type` is not pure
markdown** — i.e. anything writing `.jsonl`/`.yaml`/hybrid storage. "No
view, no promotion" joins "no eval, no promotion" in the plugin contract
(`domains/README.md`). Pure-markdown domains (`journal/`) are already
Obsidian-native and declare no `view:`.

**2. The kernel calls the registered renderer after `handler.write`.**

A new `Orchestrator` helper — `_render_domain_view(domain, started_ts)` —
runs in each `_handle_*_write` path, right next to `_maybe_refresh_index`.
It resolves the domain's `view:` declaration, lazily imports the renderer
(plugin imports stay lazy, per the plugin contract), and calls it. A small
kernel helper module reads and caches the `domain.yaml` `view:`
declarations so the resolution is data-driven — the kernel never names a
domain.

The renderer signature is uniform across every domain:

```python
def render(vault_root: Path, *, now: datetime) -> None: ...
```

The renderer is a **pure function of canonical storage** — it reads the
`.jsonl`/`.yaml`, regenerates *all* its output files, writes them through
`kernel.vault.atomic_write`. No `WriteResult` detail crosses the seam, so
the kernel primitive stays truly uniform regardless of each domain's
write-result shape.

View regeneration is **best-effort**: wrapped in `try/except`, audit-logged
as `op=view_render`, and a render failure can never roll back the
canonical write that already landed. This inherits reminder's existing
contract verbatim.

**3. File granularity: monthly rollups for logs, single-file for state.**

Append-style logs project to dated monthly files
(`finance/2026-04.md`, `fitness/workouts-2026-04.md`). Live-state domains
project to a single file (`inventory/state.md`, `fitness/metrics.md`,
`reminder/reminders.md`). This keeps file counts sane — finance's ~1,200
transactions become ~3 monthly notes, not 1,200 — and dated monthly notes
are the natural wiki nodes.

**4. Generated views carry structural `[[wikilinks]]` (Phase 1 wiki).**

Renderers emit deterministic structural links: prev/next month
(`[[finance/2026-03]]`), cross-domain same-month (`[[fitness/2026-04]]`),
and links from a monthly rollup to journal notes from the same period.
These are pure, deterministic, computed from dates and filesystem layout —
no LLM. They immediately light up `INDEX.md`'s existing
orphan/backlink/graph machinery.

Semantic journal-to-journal linking — auto-populating the journal
handler's `links: []` frontmatter field with topically related notes — is
explicitly **out of scope for this ADR**. It mutates user-authored
narrative files (hitting the 30-min user-edit buffer invariant), it is
LLM-dependent, and it is eval-relevant. It is the pre-existing issue
#4/#10 work and will get its own ADR.

**5. `index.py` and `retrieval.py` treat generated views selectively.**

`kernel/index.py` walks every `.md` under `vault/<domain>/`. It will now
encounter the generated views. It identifies them by the `generated_by:`
frontmatter marker (the same mechanism `index.py` already uses to stamp
its own output) and treats them selectively: **their `[[wikilinks]]` are
harvested into the backlink graph** (so the wiki works), but they are
**excluded from orphans, topic clusters, recent activity, and domain
stats** (so finance is not double-counted as both 1,200 jsonl rows and 3
md notes, and so `INDEX.md`'s eval-relevant sections do not shift).

`kernel/retrieval.py` has a vault-wide `rglob("*.md")` path; it gains a
one-line exclusion of `generated_by:`-marked files so a generated
projection can never be read back as if it were a source note.

## Alternatives considered

**For the mechanism (change 1–2):**

1. **Plugin-local copy of the reminder pattern.** Each plugin ships its
   own `view.py` and calls `_regenerate_view()` itself. Zero kernel
   changes — fully "add files, don't change files." Rejected: the user
   explicitly asked for a *standing contract covering all current and
   future structured domains*. A contract the kernel does not enforce is
   not a contract; it is a hope. It also duplicates the regenerate-trigger
   and JSONL-iteration logic across every domain (reminder already
   duplicates `_iter_events` between `handler.py` and `view.py`).

2. **Lazy regeneration on the index-refresh cycle.** Declare the view in
   `domain.yaml`, but regenerate it on `index.py`'s every-5-writes refresh
   rather than after every write. Cheaper. Rejected: the view would lag
   reality by up to five writes — the user logs a workout and it is not in
   Obsidian until four more writes happen. The reminder precedent already
   set the "after every write" expectation.

**For file granularity (change 3):**

1. **One file per domain.** `finance/transactions.md` etc. Dead simple,
   matches reminder exactly. Rejected: a ~1,200-row markdown table is not
   browsable, and it gives the wiki exactly one node per domain — almost
   nothing to link.

2. **Per-entity notes.** `finance/merchants/loblaws.md`,
   `inventory/items/aaa-batteries.md`. Maximum wiki-linkability. Rejected:
   large file counts, and this is the case `CLAUDE.md` pitfall #7 is most
   directly warning against. If per-entity nodes are wanted later, the
   `INDEX.md` tag map can surface entities without minting a file each.

**For `index.py` handling (change 5):**

1. **Walk generated views like any note.** Their links feed the graph for
   free. Rejected: finance is double-counted in domain stats, generated
   views pollute recent-activity and orphans, and `INDEX.md` content
   shifts — and `INDEX.md` is read first on every turn (engineering
   decision #5, `vault_index_first`), so this moves the eval baseline.

2. **Skip generated views entirely** (treat like `_audit/`). Zero eval
   impact, zero pollution. Rejected: the structural links built in change
   4 would never feed `INDEX.md`'s backlink graph — half the wiki value
   evaporates.

**For wiki scope (change 4):**

1. **Do semantic journal linking now, in one push.** More complete.
   Rejected: bundles file-mutating, LLM-dependent, eval-sensitive work
   into a projection change — a regression in the bundle could not be
   bisected to a single cause. Phasing keeps each PR reviewable.

## Consequences

**Why this is not a cardinal-rule violation.** `CLAUDE.md` says *"kernel
never changes to add a use case."* This change adds no use case. It is
shared infrastructure every structured domain consumes symmetrically: the
view primitive is generated *from* each domain's `domain.yaml`, not
hardcoded *for* any domain; the kernel never names a domain; adding a new
domain still means dropping a `domain.yaml` (now with one more required
block). It is the same escape hatch `CLAUDE.md` already grants for
cross-domain primitives — and that ADR 2026-05-10 used for the chat-context
and capabilities work.

**`CLAUDE.md` pitfall #7 is amended, not broken.** The pitfall text gains
a carve-out: markdown is still forbidden *as canonical storage* for
structured data; a generated, `generated_by:`-marked, never-read-back
projection is explicitly permitted. The reminder plugin already
established this in practice — this ADR makes it a named, enforced rule.

**Good**

- Every structured domain becomes browsable in Obsidian. Finance,
  inventory, and fitness join journal and reminders in the graph.
- The contract is enforced by the kernel, not by memory. A new structured
  domain that omits `view:` fails the plugin contract the same way one
  that omits `eval/` does.
- `INDEX.md`'s existing orphan/backlink/cluster machinery finally has
  cross-domain links to work with — the vault starts to read like a wiki.
- The kernel↔plugin seam stays clean: plugins own their `view.py` render
  logic; the kernel owns only the uniform trigger.

**Bad / things to watch**

- **`index.py` is on the off-limits list.** Change 5 edits it. This is
  deliberate and ADR-gated — the selective-walk logic is small and keyed
  off a frontmatter marker `index.py` already understands — but it is a
  kernel edit and the eval must confirm zero delta.
- **Eval baseline.** Intent is zero-delta: generated views are never read
  back (change 5 keeps them out of retrieval and out of `INDEX.md`'s
  eval-relevant sections). But `eval/run.py` must be run against both
  `configs/default.yaml` and `configs/baseline.yaml` to *confirm* it,
  before any of this is promoted.
- **Full-regen cost.** Each structured write regenerates all of that
  domain's view files. At Jason's scale (one Mac mini, dozens of monthly
  files, a few writes a day) this is a rounding error, and determinism +
  a content-compare in `atomic_write` means unchanged months produce
  byte-identical output and no Drive-sync churn. If a future domain ever
  has thousands of monthly files, revisit with targeted regeneration —
  the monthly-rollup decision already makes that unlikely.
- **Sequencing.** The work lands across several issues: schema + contract
  docs, the kernel hook + view-resolver helper, the `index.py`/
  `retrieval.py` selective handling, the three new `view.py` renderers,
  and the reminder retrofit from self-called to kernel-called. The
  renderers depend on the schema; the kernel hook depends on nothing but
  should land before the renderers are wired; the reminder retrofit lands
  last so its existing behavior is the migration's regression check.

This is a kernel-touching change, but it adds shared infrastructure every
domain consumes uniformly. No new branch in the classifier or dispatch
table, no domain-specific kernel code path, no edit to
`configs/baseline.yaml`.
