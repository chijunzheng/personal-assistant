# ADR 2026-05-09: Vault on iCloud Obsidian (not Google Drive)

**Status:** Accepted
**Date:** 2026-05-09

## Context

The vault has lived under
`~/Library/CloudStorage/GoogleDrive-chijunzheng@gmail.com/My Drive/1.Projects/personal-assistant/vault/`
since the project began. That path was convenient for desktop development —
Drive Desktop syncs the directory to drive.google.com, and the kernel's
SYNC.md was written against Drive's conflict semantics (filenames suffixed
with `(Conflict YYYY-MM-DD HH-MM)`).

The problem surfaced once mobile capture became part of the workflow:
**iPhone Obsidian cannot open the vault.** Obsidian on iOS only opens
vaults that live in iCloud Drive (the `iCloud~md~obsidian` container) or
in a folder accessible through the iOS Files app via an iCloud-backed
provider. The Google Drive iOS app is *not* such a provider for Obsidian
— it exposes files through its own UI, not through the system file
provider that Obsidian's vault picker enumerates.

Jason already keeps a populated personal Obsidian vault at
`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Jason/` (Daily
Notes, Maps of Content, Literature Notes). Mobile parity for the
assistant requires the assistant's vault to live in the same iCloud
Obsidian container so the same iOS Obsidian app can reach it.

The kernel reads its vault root from one place — `DEFAULT_VAULT_ROOT` in
`kernel/orchestrator.py:58` and again in `kernel/proactive.py:50`, both
already wired to read the `VAULT_ROOT` environment variable with a
`"vault"` fallback for tests. That pre-existing seam means this is a
configuration change, not a code change to the dispatch logic.

## Decision

Move the vault to
`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Jason/PersonalAssistant/`,
as a subfolder of Jason's existing iCloud Obsidian vault. The kernel
reads the path from the `VAULT_ROOT` environment variable (already
implemented at `kernel/orchestrator.py:58` and `kernel/proactive.py:50`).
The launchd plists in `infra/launchd/` set `VAULT_ROOT` via the existing
`EnvironmentVariables` template-substitution pattern used by the bot,
digest, conflict-watcher, and reminder-dispatch plists.

Concretely:

- The default for tests and dev remains `"vault"` (relative). No test
  fixture needs to change.
- Production launchd jobs set `VAULT_ROOT` to the absolute iCloud path.
- The folder is added as a subfolder of Jason's existing iCloud Obsidian
  vault — the assistant's writes appear under a `PersonalAssistant/`
  node in Obsidian's file tree on both Mac and iPhone.

No edits to `kernel/classifier.py`, `kernel/orchestrator.py` dispatch,
`kernel/retrieval.py`, or any plugin handler. The seam already exists.

## Alternatives considered

1. **Sibling vault at iCloud Documents root** —
   `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/PersonalAssistant/`,
   not nested under Jason's existing Obsidian vault. Rejected because
   Obsidian treats each top-level Documents folder as a separate vault.
   Jason would have to vault-switch on his phone to flip between his
   personal notes and the assistant's writes. The point of mobile
   parity is one app, one tap — not two vaults.

2. **Move Jason's existing personal vault to be the assistant vault** —
   reuse the same root. Rejected because the assistant kernel expects a
   specific layout (`journal/`, `finance/`, `inventory/`, `fitness/`,
   `_index/INDEX.md`, `_audit/*.jsonl`). Jason's personal vault contains
   Map of Contents, Daily Notes, and Literature Notes that don't match
   that layout — the kernel's `_maybe_refresh_index` would index
   thousands of unrelated personal notes into INDEX.md, and the
   classifier could pick up unrelated paths during retrieval.

3. **Symlink from the Drive path to the iCloud path** — keep the kernel
   pointed at the existing Drive directory and symlink it into iCloud.
   Rejected on two grounds: (a) iCloud's file coordinator does not
   reliably traverse symlinks across containers, and `.icloud` placeholders
   would be created against the symlink target rather than the symlink
   itself; (b) launchd's working directory is not the project directory,
   so any relative resolution through the symlink is fragile across
   restarts.

## Consequences

**Good**

- Mobile parity: capture and query from iPhone Obsidian without any
  bridge code.
- Single environment variable controls the vault root across the bot
  process and every launchd job — `VAULT_ROOT` was already the seam,
  so no kernel dispatch code changes.
- Test isolation is preserved: tests pass `vault_root=tmp_path` and
  never touch iCloud.

**Bad / things to watch**

- **Conflict-filename semantics differ.** `kernel/SYNC.md` and
  `kernel/conflict_watcher.py` were written for Drive's
  `(Conflict YYYY-MM-DD HH-MM)` suffix. iCloud uses `<filename> 2.md`
  on benign duplicates and `<host>'s conflicted copy.md` on real
  conflicts. The watcher will silently ignore iCloud conflicts until
  its filename matcher is updated. **This is a known follow-up,
  deferred from this change**, tracked separately. Until then, conflict
  recovery on the iCloud vault is manual.

- **`.icloud` placeholders.** With "Optimize Mac Storage" enabled,
  iCloud evicts file contents and leaves a metadata stub. `Path.exists()`
  returns `True` but `read_text()` returns binary metadata. Mitigation:
  keep "Optimize Mac Storage" disabled for the
  `iCloud~md~obsidian/Documents/Jason/PersonalAssistant/` subtree.
  Document this in the launchd README so future operators don't trip
  on it.

- **Audit invariant unchanged.** `vault/_audit/*.jsonl` continues to be
  the source of truth. iCloud syncs the audit log to other devices,
  which is acceptable because the audit format is append-only with
  sha256 IDs and the kernel single-instance flock keeps writes
  serialized on the one machine that runs the bot.

- This is a kernel-touching change (the environment variable affects
  `kernel/orchestrator.py` and `kernel/proactive.py`), but it is purely
  a path/configuration change. No new branch in the classifier, no
  new branch in the dispatch table, no new behavior — exactly the
  scope CLAUDE.md permits for a kernel change documented as an ADR.
