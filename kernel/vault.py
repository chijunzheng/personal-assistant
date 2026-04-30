"""Vault I/O primitives.

The vault is the on-disk memory store backed by Google Drive. Every write
that the kernel performs against vault files MUST go through ``atomic_write``
so that a syncing process (Drive Desktop, Obsidian indexer, etc.) never
observes a half-written file. See ``kernel/SYNC.md`` for the full strategy.

Two defenses live in this module:

1. **Defense 1 — atomic writes.** ``atomic_write`` writes to a sibling
   ``<path>.tmp`` then ``os.replace``s it into place. POSIX rename is
   atomic, so concurrent readers (Drive sync, Obsidian indexer) never
   observe a half-written file.

2. **Defense 3 — 30-min user-edit buffer.** When the caller passes a
   ``vault_root`` plus a ``write_buffer_min``, ``atomic_write`` first
   checks the target file's mtime; if it was modified within the buffer
   window, the proposed content is staged under
   ``<vault_root>/_inbox/_pending_edits/<relative-path>`` instead of
   overwriting in place. The user's in-flight edit is preserved; the
   agent's proposed change is queued for later review.

Callers that don't pass ``vault_root`` get the original (non-buffered)
behavior — the issue-#1 happy path is unchanged.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

__all__ = ["AtomicWriteResult", "atomic_write"]


DEFAULT_WRITE_BUFFER_MIN = 30
PENDING_EDITS_DIR = "_inbox/_pending_edits"


@dataclass(frozen=True)
class AtomicWriteResult:
    """Where the bytes actually landed.

    Attributes:
        path: the file that now contains ``content``.
        staged: ``True`` if the write was deflected to the pending-edits
            staging area because the canonical target was inside the
            user-edit buffer; ``False`` if the canonical target was
            written in place.
    """

    path: Path
    staged: bool


def _seconds_since_mtime(target: Path, *, now: float) -> Optional[float]:
    """Return age of ``target`` in seconds, or ``None`` if it does not exist."""
    try:
        st = target.stat()
    except FileNotFoundError:
        return None
    return now - st.st_mtime


def _stage_path(
    *,
    target: Path,
    vault_root: Path,
    now: float,
) -> Path:
    """Compute a unique staging path under ``<vault_root>/_inbox/_pending_edits/``.

    The relative segment mirrors the original path inside the vault so
    triage tooling can reconcile a staged edit back to its canonical
    file. A nanosecond suffix on the filename keeps rapid retries from
    clobbering each other.
    """
    try:
        rel = target.resolve().relative_to(vault_root.resolve())
    except ValueError:
        # Target is outside the vault root — fall back to the bare name
        # so we still avoid the canonical-overwrite path.
        rel = Path(target.name)

    base = vault_root / PENDING_EDITS_DIR / rel.parent
    suffix = f"{int(now * 1e9)}{target.suffix}"
    stem = target.stem
    return base / f"{stem}.{suffix}"


def _write_atomic(target: Path, content: str) -> None:
    """The original POSIX-rename atomic write — Defense 1."""
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


def atomic_write(
    path: str | os.PathLike[str],
    content: str,
    *,
    vault_root: str | os.PathLike[str] | None = None,
    write_buffer_min: int = DEFAULT_WRITE_BUFFER_MIN,
    now: Callable[[], float] | None = None,
) -> AtomicWriteResult:
    """Write ``content`` to ``path`` so no concurrent reader sees a half-written file.

    When ``vault_root`` is provided, the function additionally honors the
    30-min user-edit buffer (``kernel/SYNC.md`` defense #3): if the target
    file was modified within ``write_buffer_min`` minutes from now, the
    proposed content is staged under
    ``<vault_root>/_inbox/_pending_edits/<relative-path>`` rather than
    overwriting the canonical file. Otherwise behavior matches the
    original ``atomic_write``.

    Returns:
        An ``AtomicWriteResult`` describing where the bytes landed and
        whether staging occurred.

    Raises:
        OSError: the underlying replace/rename failed; no ``.tmp`` debris
            is left behind.
    """
    target = Path(path)
    clock = now or time.time

    # Without a vault_root we preserve the issue-#1 contract verbatim.
    if vault_root is None:
        _write_atomic(target, content)
        return AtomicWriteResult(path=target, staged=False)

    vault = Path(vault_root)
    current = clock()
    age = _seconds_since_mtime(target, now=current)
    buffer_seconds = max(0, write_buffer_min) * 60

    if age is not None and age < buffer_seconds:
        staged = _stage_path(target=target, vault_root=vault, now=current)
        _write_atomic(staged, content)
        return AtomicWriteResult(path=staged, staged=True)

    _write_atomic(target, content)
    return AtomicWriteResult(path=target, staged=False)
