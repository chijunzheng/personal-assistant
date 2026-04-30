"""Vault I/O primitives.

The vault is the on-disk memory store backed by Google Drive. Every write
that the kernel performs against vault files MUST go through ``atomic_write``
so that a syncing process (Drive Desktop, Obsidian indexer, etc.) never
observes a half-written file. See ``kernel/SYNC.md`` for the full strategy.

This module is intentionally small. The 30-minute mtime guard described in
``kernel/SYNC.md`` defense #3 is deferred to issue #11; only the atomic
write primitive plus a couple of lightweight read helpers ship in #1.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["atomic_write"]


def atomic_write(path: str | os.PathLike[str], content: str) -> None:
    """Write ``content`` to ``path`` so no concurrent reader sees a half-written file.

    Implementation detail (not part of the public contract): writes to a
    sibling ``<path>.tmp`` file then ``os.replace``s it into place. Callers
    should treat this function as "the file either contains the new
    content or the old content, never something in between."

    Parent directories are created as needed.
    """
    target = Path(path)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    tmp = target.with_name(target.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        # Best effort: if we crashed after creating tmp but before replacing,
        # leave no stray ``.tmp`` lying around for the next caller / for Drive
        # to pick up and sync.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
