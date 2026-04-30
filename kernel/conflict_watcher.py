"""Drive-conflict watcher (``kernel/SYNC.md`` defense #4).

Google Drive Desktop renames a file ``<name> (Conflict YYYY-MM-DD HH-MM).md``
when it cannot decide whose write wins. This module:

  1. Globs the vault for any such files (``run_once``).
  2. For each, reads canonical + conflict, computes a line-level diff.
  3. If ``conflict_auto_merge`` is enabled (engineered config), invokes
     an injected LLM merger and atomically writes the result back to
     the canonical path; the conflict file is deleted on success.
  4. Otherwise (baseline config), moves the conflict file unchanged into
     ``vault/_inbox/_conflicts/`` and leaves the canonical untouched.
  5. In both branches notifies the user via an injected callback and
     audit-logs a ``conflict_resolve`` op carrying the config flag value.

The module exposes a single ``ConflictWatcher`` class. Production wiring
(launchd plist) constructs one instance and calls ``run_loop`` so the
process stays resident; tests construct an instance and call ``run_once``
with a temporary vault.

The watcher is single-machine by design — see ``kernel/SYNC.md`` "What
this strategy does NOT cover". No locking across hosts is attempted.
"""

from __future__ import annotations

import difflib
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from kernel.audit import write_audit_entry
from kernel.vault import atomic_write

__all__ = [
    "ConflictResolution",
    "ConflictWatcher",
    "MergerFn",
    "NotifierFn",
]


logger = logging.getLogger(__name__)


# Drive's pattern: ``<original-stem> (Conflict YYYY-MM-DD HH-MM).md``.
# We accept the loose space-separated form Drive emits — date / time
# segments contain digits, dashes, and spaces, but no parentheses.
_CONFLICT_PATTERN = re.compile(r"^(?P<stem>.+?) \(Conflict [^)]+\)$")
_CONFLICT_GLOB = "**/* (Conflict *).md"

# Where unmerged conflicts are staged when auto-merge is off.
_INBOX_CONFLICTS = Path("_inbox/_conflicts")


# Public callable types so tests and downstream wiring agree on shape.
MergerFn = Callable[..., str]
"""Signature: ``merger(*, canonical_text, conflict_text, diff) -> merged``."""

NotifierFn = Callable[[str], None]
"""Signature: ``notifier(message)``."""


@dataclass(frozen=True)
class ConflictResolution:
    """One conflict's outcome — useful for tests + run-loop summaries."""

    conflict_path: Path
    canonical_path: Path
    merged: bool
    staged_path: Optional[Path]


def _is_in_conflicts_inbox(path: Path, vault_root: Path) -> bool:
    """Skip files we already moved into the inbox in a prior run."""
    inbox = (vault_root / _INBOX_CONFLICTS).resolve()
    try:
        return inbox in path.resolve().parents
    except OSError:
        return False


def _canonical_for(conflict_path: Path) -> Optional[Path]:
    """Return the canonical sibling for a Drive-conflict file, or ``None``.

    Drive names the conflict ``<stem> (Conflict ...).md`` next to
    ``<stem>.md``. We strip the suffix and look in the same directory.
    """
    stem_match = _CONFLICT_PATTERN.match(conflict_path.stem)
    if stem_match is None:
        return None
    canonical_stem = stem_match.group("stem")
    canonical = conflict_path.with_name(f"{canonical_stem}{conflict_path.suffix}")
    return canonical


def _structural_diff(canonical_text: str, conflict_text: str) -> str:
    """Cheap line-level unified diff used as input to the LLM merger."""
    canonical_lines = canonical_text.splitlines(keepends=True)
    conflict_lines = conflict_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        canonical_lines,
        conflict_lines,
        fromfile="canonical",
        tofile="conflict",
        lineterm="",
    )
    return "".join(diff)


def _stage_destination(conflict_path: Path, vault_root: Path) -> Path:
    """Compute the per-conflict landing path under ``_inbox/_conflicts/``.

    We mirror the original relative directory structure under the inbox
    so triage tooling can correlate. A timestamp suffix on the filename
    keeps repeated stagings of the same conflict from clobbering.
    """
    try:
        rel = conflict_path.resolve().relative_to(vault_root.resolve())
    except ValueError:
        rel = Path(conflict_path.name)
    suffix = f".{int(time.time() * 1e6)}"
    return vault_root / _INBOX_CONFLICTS / rel.parent / f"{conflict_path.stem}{suffix}{conflict_path.suffix}"


class ConflictWatcher:
    """Glob, diff, merge-or-stage, notify, audit. See module docstring."""

    def __init__(
        self,
        *,
        vault_root: str | Path,
        audit_root: str | Path,
        config_label: str,
        conflict_auto_merge: bool,
        merger: MergerFn,
        notifier: NotifierFn,
        clock: Optional[Callable[[], datetime]] = None,
        audit_writer: Optional[Callable[..., object]] = None,
    ) -> None:
        self._vault_root = Path(vault_root)
        self._audit_root = Path(audit_root)
        self._config_label = config_label
        self._auto_merge = bool(conflict_auto_merge)
        self._merger = merger
        self._notifier = notifier
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self._audit_writer = audit_writer or write_audit_entry

    # -- public ---------------------------------------------------------------

    def run_once(self) -> list[ConflictResolution]:
        """Scan the vault and resolve every conflict file found."""
        if not self._vault_root.exists():
            return []

        results: list[ConflictResolution] = []
        for conflict_path in self._iter_conflicts():
            try:
                results.append(self._handle_conflict(conflict_path))
            except Exception as err:  # noqa: BLE001 — bound at module boundary
                logger.exception("conflict resolution failed: %s", conflict_path)
                self._audit("error", conflict_path=conflict_path, error=str(err))
        return results

    def run_loop(self, interval_min: int) -> None:  # pragma: no cover — daemon path
        """Repeatedly call ``run_once`` every ``interval_min`` minutes.

        Production wiring uses this from the launchd plist (#12); test
        coverage targets ``run_once`` directly so we don't have to
        block on real sleeps. Marked ``no cover`` for that reason.
        """
        if interval_min <= 0:
            raise ValueError("interval_min must be positive")
        interval_sec = interval_min * 60
        while True:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("conflict watcher run_once raised")
            time.sleep(interval_sec)

    # -- private --------------------------------------------------------------

    def _iter_conflicts(self) -> Iterable[Path]:
        """Yield every Drive-conflict file under ``vault_root``, skipping the inbox."""
        for path in self._vault_root.glob(_CONFLICT_GLOB):
            if not path.is_file():
                continue
            if _is_in_conflicts_inbox(path, self._vault_root):
                continue
            yield path

    def _handle_conflict(self, conflict_path: Path) -> ConflictResolution:
        """Dispatch to merge or stage based on config. Always notify + audit."""
        canonical_path = _canonical_for(conflict_path)

        if canonical_path is None:
            # Pattern matched the glob but not the precise regex; stage the
            # stray file out of the way without touching anything else.
            return self._resolve_by_staging(conflict_path, conflict_path)

        if self._auto_merge:
            return self._resolve_by_merging(conflict_path, canonical_path)

        return self._resolve_by_staging(conflict_path, canonical_path)

    def _resolve_by_merging(
        self,
        conflict_path: Path,
        canonical_path: Path,
    ) -> ConflictResolution:
        """LLM-merge branch: invoke merger, write canonical, delete conflict."""
        canonical_text = (
            canonical_path.read_text(encoding="utf-8")
            if canonical_path.exists()
            else ""
        )
        conflict_text = conflict_path.read_text(encoding="utf-8")
        diff = _structural_diff(canonical_text, conflict_text)
        merged_text = self._merger(
            canonical_text=canonical_text,
            conflict_text=conflict_text,
            diff=diff,
        )
        # The watcher writes the canonical without the user-edit buffer
        # guard: by definition the merged text *contains* whatever recent
        # user edits the canonical held, so blocking the write would leave
        # the conflict unresolved on disk and re-trigger every minute. The
        # 30-min buffer protects the agent from clobbering user content;
        # the watcher is preserving it.
        atomic_write(canonical_path, merged_text)
        try:
            conflict_path.unlink()
        except FileNotFoundError:
            pass

        self._notify(canonical_path, merged=True)
        self._audit(
            "ok",
            conflict_path=conflict_path,
            canonical_path=canonical_path,
            merged=True,
            staged_path=None,
        )
        return ConflictResolution(
            conflict_path=conflict_path,
            canonical_path=canonical_path,
            merged=True,
            staged_path=None,
        )

    def _resolve_by_staging(
        self,
        conflict_path: Path,
        canonical_path: Path,
    ) -> ConflictResolution:
        """Baseline branch: move conflict unchanged into ``_inbox/_conflicts/``."""
        staged = self._stage_unchanged(conflict_path)
        self._notify(canonical_path, merged=False)
        self._audit(
            "ok",
            conflict_path=conflict_path,
            canonical_path=canonical_path,
            merged=False,
            staged_path=staged,
        )
        return ConflictResolution(
            conflict_path=conflict_path,
            canonical_path=canonical_path,
            merged=False,
            staged_path=staged,
        )

    def _stage_unchanged(self, conflict_path: Path) -> Path:
        """Move ``conflict_path`` under ``_inbox/_conflicts/`` without modifying it."""
        destination = _stage_destination(conflict_path, self._vault_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(conflict_path), str(destination))
        return destination

    def _notify(self, file_path: Path, *, merged: bool) -> None:
        """Send a single message describing what happened to a conflict.

        ``file_path`` is the canonical file (so the user recognizes the
        affected note) when one could be derived; otherwise the conflict
        file itself.
        """
        verb = "auto-merged" if merged else "staged for review"
        message = f"Drive conflict {verb}: {file_path.name}"
        try:
            self._notifier(message)
        except Exception:  # noqa: BLE001
            logger.exception("notifier raised for %s", file_path)

    def _audit(
        self,
        outcome: str,
        *,
        conflict_path: Path,
        canonical_path: Optional[Path] = None,
        merged: bool = False,
        staged_path: Optional[Path] = None,
        error: Optional[str] = None,
    ) -> None:
        """Append one ``conflict_resolve`` entry to the audit log."""
        ts = self._clock()
        entry: dict[str, object] = {
            "ts": ts.isoformat(),
            "op": "conflict_resolve",
            "actor": "kernel.conflict_watcher",
            "outcome": outcome,
            "duration_ms": 0,
            "config": self._config_label,
            "path": str(conflict_path),
            "merged": merged,
        }
        if canonical_path is not None:
            entry["canonical_path"] = str(canonical_path)
        if staged_path is not None:
            entry["staged_path"] = str(staged_path)
        if error is not None:
            entry["error"] = error
        try:
            self._audit_writer(entry, audit_root=self._audit_root)
        except Exception:  # noqa: BLE001
            logger.exception("audit write failed for %s", conflict_path)
