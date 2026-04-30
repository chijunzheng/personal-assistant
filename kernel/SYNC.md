# Sync & Concurrency — Living with Google Drive Desktop

The vault sits in Drive Desktop's synced folder. This file documents how the
kernel handles the resulting concurrency surface, and what assumptions kernel
code MUST honor to stay safe.

## Deployment assumptions

- **One agent, one Mac mini.** The kernel runs on exactly one machine.
- **Phone Obsidian is mostly read-only.** Editing happens occasionally; the
  primary mobile interface is Telegram.
- **Drive Desktop syncs near-real-time** (typically <5s) but is NOT
  synchronous. There is always a window where local and remote disagree.
- **Drive's conflict resolution** is last-write-wins by mtime, with a
  `<filename> (Conflict YYYY-MM-DD HH-MM).md` file when it can't decide.

## The five defenses, in priority order

### Defense 1 — Atomic writes (REQUIRED for all kernel writes)

Every write to a vault file MUST go through `kernel/vault.py:atomic_write`,
which writes to `<path>.tmp` then `os.replace()`s into place. POSIX rename is
atomic — no partial-file window can be observed by a syncing process.

**Rule:** No kernel code may call `open(path, 'w')` directly on a vault file.

### Defense 2 — Append-only + content-hash idempotency

Files that accumulate events are append-only:
- `vault/finance/transactions.jsonl`
- `vault/inventory/events.jsonl`
- `vault/reminder/events.jsonl`
- `vault/_audit/<date>.jsonl`

Every event has an `id: sha256` field derived from its content. Readers MUST
dedupe by `id` on load. This makes appends **commutative** — two writers
appending the same event produces a duplicate that's silently dropped on
read, not a corruption.

**Rule:** Event-log writes are appends only; never edits or rewrites.

### Defense 3 — "Don't touch user-recent" rule

Before modifying any narrative `.md` file in `vault/journal/` (or any future
narrative-style domain), the kernel checks the file's mtime. If mtime is
within `WRITE_BUFFER_MIN` (default 30 min) of now, the kernel will NOT
modify the file. Instead, it falls back to one of:

- **Create new** — write a fresh file with a related slug
- **Append to agent zone** — files have a designated `## Auto-captured`
  section; agent appends there only, never edits user content
- **Stage to pending** — write the proposed change to
  `vault/_inbox/_pending_edits/<original-path>` for review

**Rule:** No narrative-file modification crosses the 30-min window check.

This is the highest-leverage defense for collision avoidance. Implement it
in `kernel/vault.py` as a guard around every modify-existing operation.

### Defense 4 — Conflict watcher daemon

A small launchd job runs `kernel/conflict_watcher.py` every minute:

```
1. Glob vault/**/*"(Conflict "*.md  (Drive's naming pattern)
2. For each conflict file:
     original = strip "(Conflict ...)" from filename
     a = read original (current "winning" version)
     b = read conflict file
     diff = structural_diff(a, b)
     if diff is purely additive (b adds lines, a unchanged):
         merged = LLM_merge_or_concatenate(a, b)
         atomic_write(original, merged)
         move(conflict_file, vault/_audit/_conflicts_resolved/)
     else:
         move both into vault/_inbox/_conflicts/<original-name>/
         telegram.send("Conflict on <file>. Reply: keep-A | keep-B | merge")
3. Audit-log every action.
```

If `suggested_actions` engineering bool is ON, the LLM merge is permitted.
If OFF (baseline), conflict watcher only stages and notifies — no auto-merge.
This makes conflict-merge another measurable engineering decision.

### Defense 5 — Audit log enables reconstruction

Every kernel write logs to `vault/_audit/<YYYY-MM-DD>.jsonl`:

```jsonl
{"ts":"2026-04-29T22:47:01Z","op":"write","path":"vault/journal/...","sha256":"abc...","size":1247,"reason":"telegram_msg_8821","by":"kernel.vault"}
```

If a file goes missing or appears corrupt, audit log replays the last known
content. The audit log is itself append-only with sha256-keyed entries, so
it's robust against its own conflicts.

**Rule:** No vault write happens without an audit-log line.

## What this strategy does NOT cover

- **Multi-agent setups** — if you ever run the kernel on two machines
  concurrently, Defense 3 alone won't save you (both agents see the file as
  "old" simultaneously). Single-agent is an architectural assumption.
- **Drive deciding to delete a file** — extremely rare but possible if Drive
  thinks a remote tombstone is authoritative. Defense 5 lets you recover by
  rewriting from audit log.
- **Long Drive sync outages** — if Drive sync stalls for hours and you edit
  on phone meanwhile, eventual reconciliation may produce conflict files
  that Defense 4 handles, but the user experience is degraded for that
  window.

## Settings (in configs/default.yaml — to be added)

```yaml
sync:
  write_buffer_min: 30            # Defense 3: agent waits this long after user edit
  conflict_watcher_interval_min: 1
  conflict_auto_merge: true       # gated on engineering decision suggested_actions
  audit_required: true            # Defense 5: no write without audit
```

## Kernel implementation checklist

Before any kernel code that writes to vault is merged:

- [ ] Uses `kernel/vault.py:atomic_write` (Defense 1)
- [ ] Event-log writes are appends with sha256 IDs (Defense 2)
- [ ] Modify-existing operations check `mtime_within_buffer()` (Defense 3)
- [ ] Audit log entry written before/after the data write (Defense 5)
- [ ] Has a unit test for the dedupe-on-read path (Defense 2)
- [ ] Has a unit test for the buffer-window guard (Defense 3)
