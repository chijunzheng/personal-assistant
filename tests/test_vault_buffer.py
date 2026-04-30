"""Tests for the 30-min user-edit buffer (``kernel/SYNC.md`` defense #3).

These verify external behavior: when the target file's mtime is recent
(within ``write_buffer_min``), the kernel must NOT overwrite it; instead
it stages the proposed content under
``vault/_inbox/_pending_edits/<original-relative-path>`` so the user's
in-flight edit isn't clobbered. When mtime is older than the buffer (or
the file doesn't exist), behavior matches the pre-buffer happy path.

Tests use injected ``now`` and ``mtime_resolver`` callables so we never
have to ``time.sleep`` to age a file.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kernel.vault import atomic_write


def test_atomic_write_routes_to_canonical_when_mtime_outside_buffer(
    tmp_path: Path,
) -> None:
    """An older file (mtime > buffer ago) is overwritten in place — original semantics."""
    vault_root = tmp_path
    target = vault_root / "journal" / "2026-04-29.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old content\n", encoding="utf-8")

    # Push the file's mtime to two hours ago — well outside the 30-min buffer.
    long_ago = time.time() - 2 * 3600
    os.utime(target, (long_ago, long_ago))

    result = atomic_write(
        target,
        "fresh content\n",
        vault_root=vault_root,
        write_buffer_min=30,
    )

    assert target.read_text(encoding="utf-8") == "fresh content\n"
    assert result.path == target
    assert result.staged is False


def test_atomic_write_stages_when_mtime_inside_buffer(tmp_path: Path) -> None:
    """A file the user edited 5 min ago must NOT be overwritten — stage instead."""
    vault_root = tmp_path
    target = vault_root / "journal" / "2026-04-29.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    user_content = "user is mid-thought...\n"
    target.write_text(user_content, encoding="utf-8")

    # Simulate the user having edited 5 minutes ago — well within a 30-min buffer.
    five_min_ago = time.time() - 5 * 60
    os.utime(target, (five_min_ago, five_min_ago))

    result = atomic_write(
        target,
        "agent's proposed rewrite\n",
        vault_root=vault_root,
        write_buffer_min=30,
    )

    # Canonical file is untouched; agent content lives in pending-edits.
    assert target.read_text(encoding="utf-8") == user_content
    assert result.staged is True
    assert result.path != target
    assert result.path.read_text(encoding="utf-8") == "agent's proposed rewrite\n"
    # Staging path lives under the vault's pending-edits directory.
    assert (vault_root / "_inbox" / "_pending_edits") in result.path.parents


def test_atomic_write_staging_paths_are_unique_across_rapid_retries(
    tmp_path: Path,
) -> None:
    """Two near-simultaneous staging attempts must not clobber each other."""
    vault_root = tmp_path
    target = vault_root / "journal" / "2026-04-29.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("user content\n", encoding="utf-8")

    # Recent edit — both writes will route to staging.
    recent = time.time() - 60
    os.utime(target, (recent, recent))

    # Inject a deterministic clock that returns two distinct timestamps so
    # the two stage paths differ even on platforms where ``time.time`` has
    # coarser resolution than nanoseconds.
    timestamps = iter([1_700_000_000.123456789, 1_700_000_000.987654321])
    clock: list[float] = []

    def fake_now() -> float:
        value = next(timestamps)
        clock.append(value)
        return value

    first = atomic_write(
        target,
        "first proposal\n",
        vault_root=vault_root,
        write_buffer_min=30,
        now=fake_now,
    )
    second = atomic_write(
        target,
        "second proposal\n",
        vault_root=vault_root,
        write_buffer_min=30,
        now=fake_now,
    )

    assert first.staged is True
    assert second.staged is True
    assert first.path != second.path
    assert first.path.read_text(encoding="utf-8") == "first proposal\n"
    assert second.path.read_text(encoding="utf-8") == "second proposal\n"


def test_atomic_write_creates_new_canonical_when_target_missing(
    tmp_path: Path,
) -> None:
    """A non-existent target has no mtime to guard — write canonically."""
    vault_root = tmp_path
    target = vault_root / "journal" / "fresh.md"

    result = atomic_write(
        target,
        "first time\n",
        vault_root=vault_root,
        write_buffer_min=30,
    )

    assert target.read_text(encoding="utf-8") == "first time\n"
    assert result.path == target
    assert result.staged is False
