"""INDEX.md scaffold writer (minimal — issue #4 fills in the real schema).

The full INDEX schema (topic clusters, synonyms, recent activity, orphan
detection, vocabulary frontier) is the work of issue #4. Issue #2 only
needs a placeholder file at ``vault/_index/INDEX.md`` so:

  - the next agent has a concrete file to extend rather than scaffold
    from scratch
  - the journal handler can rely on ``vault/_index/`` existing as a
    real directory
  - retrieval (issue #10) can read the file even when no real index
    has been generated yet, treating it as an empty hint set

The kernel emits this file via ``vault.atomic_write`` so Drive sync never
sees a half-written placeholder.
"""

from __future__ import annotations

import os
from pathlib import Path

from kernel.vault import atomic_write

__all__ = ["INDEX_RELATIVE_PATH", "write_scaffold"]


INDEX_RELATIVE_PATH = Path("_index") / "INDEX.md"

_SCAFFOLD = """\
# Vault INDEX

This file is the auto-maintained table of contents for the vault. Issue #4
will populate it with topic clusters, synonyms, recent activity, orphan
detection, and the vocabulary frontier used to seed keyword expansion at
query time.

For now this is an intentional scaffold: the kernel writes it on startup so
later passes can extend it without checking-and-creating.

## Topic clusters

(empty — populated by ``kernel.index`` in issue #4)

## Recent activity

(empty — populated by ``kernel.index`` in issue #4)

## Orphans

(empty — populated by ``kernel.index`` in issue #4)
"""


def write_scaffold(vault_root: str | os.PathLike[str]) -> Path:
    """Write the placeholder INDEX.md to ``<vault>/_index/INDEX.md``.

    Idempotent: re-running with the same vault root produces identical
    content.
    """
    target = Path(vault_root) / INDEX_RELATIVE_PATH
    atomic_write(target, _SCAFFOLD)
    return target
