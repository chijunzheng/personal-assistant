"""Tests for ``kernel.conflict_watcher`` — Drive-conflict detection + resolution.

External behavior only:

  - Conflict files matching Drive's ``"<name> (Conflict <ts>).md"`` pattern
    are picked up under ``vault/`` regardless of nesting depth.
  - When ``conflict_auto_merge`` is on the LLM merger is consulted and the
    canonical file is overwritten via ``vault.atomic_write``; the original
    conflict file is removed.
  - When ``conflict_auto_merge`` is off the conflict file is moved to
    ``vault/_inbox/_conflicts/`` and the canonical file is preserved.
  - Both branches notify the user via the injected callback exactly once
    per conflict and audit-log a ``conflict_resolve`` op tagged with the
    config flag value.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from kernel.conflict_watcher import ConflictWatcher


def _make_clock(ts: datetime):
    """Deterministic clock factory for the watcher under test."""

    def clock() -> datetime:
        return ts

    return clock


def _audit_lines(audit_root: Path) -> list[dict]:
    """Read all JSONL audit entries written into ``audit_root``."""
    lines: list[dict] = []
    for path in sorted(audit_root.glob("*.jsonl")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                lines.append(json.loads(raw))
    return lines


def test_run_once_resolves_conflict_via_llm_merge_when_enabled(tmp_path: Path) -> None:
    """auto-merge=true: LLM merge is invoked, canonical overwritten, conflict deleted."""
    vault = tmp_path / "vault"
    audit = tmp_path / "audit"
    canonical = vault / "journal" / "2026-04-29.md"
    conflict = vault / "journal" / "2026-04-29 (Conflict 2026-04-29 18-21).md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("# canonical\nuser line\n", encoding="utf-8")
    conflict.write_text("# canonical\nphone line\n", encoding="utf-8")

    notified: list[str] = []
    merge_inputs: list[dict] = []

    def fake_merger(*, canonical_text: str, conflict_text: str, diff: str) -> str:
        merge_inputs.append(
            {
                "canonical": canonical_text,
                "conflict": conflict_text,
                "diff": diff,
            }
        )
        return "# canonical\nuser line\nphone line\n"

    watcher = ConflictWatcher(
        vault_root=vault,
        audit_root=audit,
        config_label="default",
        conflict_auto_merge=True,
        merger=fake_merger,
        notifier=lambda message: notified.append(message),
        clock=_make_clock(datetime(2026, 4, 29, 18, 30, tzinfo=timezone.utc)),
    )

    resolutions = watcher.run_once()

    assert len(resolutions) == 1
    assert canonical.read_text(encoding="utf-8") == "# canonical\nuser line\nphone line\n"
    assert not conflict.exists()
    assert len(merge_inputs) == 1
    assert "phone line" in merge_inputs[0]["conflict"]
    assert len(notified) == 1
    assert "2026-04-29.md" in notified[0]

    audit_entries = _audit_lines(audit)
    conflict_entries = [e for e in audit_entries if e["op"] == "conflict_resolve"]
    assert len(conflict_entries) == 1
    assert conflict_entries[0]["config"] == "default"
    assert conflict_entries[0]["outcome"] == "ok"


def test_run_once_stages_conflict_without_merging_when_disabled(tmp_path: Path) -> None:
    """auto-merge=false (baseline): conflict moves to inbox; canonical untouched."""
    vault = tmp_path / "vault"
    audit = tmp_path / "audit"
    canonical = vault / "journal" / "2026-04-29.md"
    conflict = vault / "journal" / "2026-04-29 (Conflict 2026-04-29 18-21).md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical_text = "# canonical\nuser line\n"
    canonical.write_text(canonical_text, encoding="utf-8")
    conflict.write_text("# canonical\nphone line\n", encoding="utf-8")

    notified: list[str] = []

    def fail_merger(**_kwargs: object) -> str:  # pragma: no cover — must not run
        raise AssertionError("merger must not be called when auto-merge is off")

    watcher = ConflictWatcher(
        vault_root=vault,
        audit_root=audit,
        config_label="baseline",
        conflict_auto_merge=False,
        merger=fail_merger,
        notifier=lambda message: notified.append(message),
        clock=_make_clock(datetime(2026, 4, 29, 18, 30, tzinfo=timezone.utc)),
    )

    resolutions = watcher.run_once()

    assert len(resolutions) == 1
    # Canonical is preserved bit-for-bit.
    assert canonical.read_text(encoding="utf-8") == canonical_text
    # Conflict file is gone from its original location.
    assert not conflict.exists()
    # And it landed under the staging inbox.
    staged_files = list((vault / "_inbox" / "_conflicts").rglob("*.md"))
    assert len(staged_files) == 1
    assert "phone line" in staged_files[0].read_text(encoding="utf-8")
    # User was notified once.
    assert len(notified) == 1
    # Audit-log line is tagged with the baseline config.
    audit_entries = _audit_lines(audit)
    conflict_entries = [e for e in audit_entries if e["op"] == "conflict_resolve"]
    assert len(conflict_entries) == 1
    assert conflict_entries[0]["config"] == "baseline"
    assert conflict_entries[0]["merged"] is False


def test_run_once_skips_already_staged_conflicts_on_subsequent_runs(
    tmp_path: Path,
) -> None:
    """Once a conflict has been moved into the inbox, the watcher must not re-process it."""
    vault = tmp_path / "vault"
    audit = tmp_path / "audit"
    canonical = vault / "journal" / "2026-04-29.md"
    conflict = vault / "journal" / "2026-04-29 (Conflict 2026-04-29 18-21).md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("user content\n", encoding="utf-8")
    conflict.write_text("phone content\n", encoding="utf-8")

    notified: list[str] = []
    watcher = ConflictWatcher(
        vault_root=vault,
        audit_root=audit,
        config_label="baseline",
        conflict_auto_merge=False,
        merger=lambda **_kw: "",
        notifier=lambda m: notified.append(m),
        clock=_make_clock(datetime(2026, 4, 29, 18, 30, tzinfo=timezone.utc)),
    )

    first = watcher.run_once()
    second = watcher.run_once()

    assert len(first) == 1
    assert len(second) == 0  # already staged; no work to do
    assert len(notified) == 1


def test_run_once_finds_conflicts_at_arbitrary_depth(tmp_path: Path) -> None:
    """Glob must descend into nested vault directories, not just the top level."""
    vault = tmp_path / "vault"
    audit = tmp_path / "audit"
    deep_dir = vault / "fitness" / "logs" / "2026" / "04"
    canonical = deep_dir / "workouts.md"
    conflict = deep_dir / "workouts (Conflict 2026-04-29 09-15).md"
    deep_dir.mkdir(parents=True, exist_ok=True)
    canonical.write_text("# workouts\n", encoding="utf-8")
    conflict.write_text("# workouts\nphone entry\n", encoding="utf-8")

    notified: list[str] = []
    watcher = ConflictWatcher(
        vault_root=vault,
        audit_root=audit,
        config_label="baseline",
        conflict_auto_merge=False,
        merger=lambda **_kw: "",
        notifier=lambda m: notified.append(m),
        clock=_make_clock(datetime(2026, 4, 29, 9, 30, tzinfo=timezone.utc)),
    )

    resolutions = watcher.run_once()

    assert len(resolutions) == 1
    assert resolutions[0].canonical_path == canonical


def test_run_once_returns_empty_list_when_vault_root_missing(tmp_path: Path) -> None:
    """Missing vault directory is a no-op — never raise on first launchd boot."""
    vault = tmp_path / "vault-not-yet-created"
    audit = tmp_path / "audit"

    watcher = ConflictWatcher(
        vault_root=vault,
        audit_root=audit,
        config_label="default",
        conflict_auto_merge=True,
        merger=lambda **_kw: "",
        notifier=lambda _m: None,
        clock=_make_clock(datetime(2026, 4, 29, 0, 0, tzinfo=timezone.utc)),
    )

    assert watcher.run_once() == []


def test_run_loop_rejects_non_positive_interval(tmp_path: Path) -> None:
    """A misconfigured launchd plist must fail loudly, not spin a tight loop."""
    watcher = ConflictWatcher(
        vault_root=tmp_path,
        audit_root=tmp_path,
        config_label="default",
        conflict_auto_merge=True,
        merger=lambda **_kw: "",
        notifier=lambda _m: None,
    )

    with pytest.raises(ValueError):
        watcher.run_loop(0)


def test_audit_records_error_when_merger_raises(tmp_path: Path) -> None:
    """A failing LLM merger must produce an audit-logged error — not crash run_once."""
    vault = tmp_path / "vault"
    audit = tmp_path / "audit"
    canonical = vault / "journal" / "2026-04-29.md"
    conflict = vault / "journal" / "2026-04-29 (Conflict 2026-04-29 18-21).md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("# canonical\n", encoding="utf-8")
    conflict.write_text("# canonical\nextra\n", encoding="utf-8")

    notified: list[str] = []

    def boom(**_kwargs: object) -> str:
        raise RuntimeError("LLM unavailable")

    watcher = ConflictWatcher(
        vault_root=vault,
        audit_root=audit,
        config_label="default",
        conflict_auto_merge=True,
        merger=boom,
        notifier=lambda m: notified.append(m),
        clock=_make_clock(datetime(2026, 4, 29, 18, 30, tzinfo=timezone.utc)),
    )

    # Must not raise out of run_once — that would crash the launchd job.
    watcher.run_once()

    audit_entries = _audit_lines(audit)
    error_entries = [
        e for e in audit_entries if e["op"] == "conflict_resolve" and e["outcome"] == "error"
    ]
    assert len(error_entries) == 1
    assert "LLM unavailable" in error_entries[0]["error"]
    # Canonical and conflict files are both still present — nothing was lost.
    assert canonical.exists()
    assert conflict.exists()
