"""Tests for orchestrator -> index.refresh trigger every N writes.

The orchestrator counts vault writes since the last refresh in a small
persistent file at ``vault/_index/.refresh_state.json``. When the count
hits ``config.context_engineering.index_refresh_after_writes`` (default 5),
the orchestrator calls ``kernel.index.refresh`` inline and audit-logs the
refresh as ``op=index_refresh``. The counter resets after a successful
refresh.

Reads, classify ops, and echoes do NOT increment the write counter — only
operations that actually persist new vault content (``journal.capture`` in
v1) do.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from kernel.classifier import Classifier
from kernel.claude_runner import ClaudeResponse
from kernel.orchestrator import Orchestrator, SingleInstanceLock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_invoker(text: str = "stubbed", tokens_in: int = 1, tokens_out: int = 1):
    """Build a minimal ``claude_runner.invoke``-shaped stub."""

    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def _build_classifier(domains_root: Path, intent: str) -> Classifier:
    """A Classifier whose LLM call always returns the given intent label."""
    return Classifier(
        domains_root=domains_root,
        invoker=_stub_invoker(text=intent),
        prompt_template="",
    )


def _seed_journal_domain(domains_root: Path) -> None:
    """Drop a journal domain.yaml so the classifier knows the intent."""
    domain_dir = domains_root / "journal"
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "domain.yaml").write_text(
        "name: journal\n"
        "description: \"narrative\"\n"
        "intents:\n"
        "  - journal.capture\n"
        "  - journal.query\n",
        encoding="utf-8",
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def _build_orchestrator(
    *,
    vault_root: Path,
    domains_root: Path,
    audit_root: Path,
    lock_path: Path,
    refresh_threshold: int = 5,
    refresh_spy: Optional[list] = None,
) -> Orchestrator:
    """Build an Orchestrator wired with a refresh-spy and journal classifier.

    ``refresh_spy`` (optional) is appended to whenever
    ``kernel.index.refresh`` would normally be called — wired through the
    orchestrator's pluggable index_refresh hook so tests don't have to
    patch a kernel module.
    """
    classifier = _build_classifier(domains_root, "journal.capture")

    refresh_kwargs: dict = {}
    if refresh_spy is not None:
        from kernel.index import RefreshResult, refresh as real_refresh

        def _spy_refresh(*args, **kwargs):
            result = real_refresh(*args, **kwargs)
            refresh_spy.append(result)
            return result

        refresh_kwargs["index_refresh"] = _spy_refresh

    config = {
        "context_engineering": {
            "index_refresh_after_writes": refresh_threshold,
        }
    }

    return Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        config=config,
        **refresh_kwargs,
    )


def _audit_entries(audit_root: Path) -> list[dict]:
    """All audit entries written by the orchestrator across the day's logs."""
    entries: list[dict] = []
    for daily in sorted(audit_root.glob("*.jsonl")):
        for raw in daily.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                entries.append(json.loads(raw))
    return entries


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_writes_below_threshold_do_not_trigger_refresh(
    tmp_path: Path, lock_path: Path
) -> None:
    """4 writes against a threshold of 5 should NOT call refresh."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    audit_root = vault_root / "_audit"
    _seed_journal_domain(domains_root)

    spy: list = []
    orch = _build_orchestrator(
        vault_root=vault_root,
        domains_root=domains_root,
        audit_root=audit_root,
        lock_path=lock_path,
        refresh_threshold=5,
        refresh_spy=spy,
    )

    for i in range(4):
        orch.handle_message(f"thought number {i}")

    assert spy == []
    refresh_entries = [e for e in _audit_entries(audit_root) if e["op"] == "index_refresh"]
    assert refresh_entries == []


def test_fifth_write_triggers_inline_refresh(tmp_path: Path, lock_path: Path) -> None:
    """The 5th write hits the threshold and calls index.refresh once."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    audit_root = vault_root / "_audit"
    _seed_journal_domain(domains_root)

    spy: list = []
    orch = _build_orchestrator(
        vault_root=vault_root,
        domains_root=domains_root,
        audit_root=audit_root,
        lock_path=lock_path,
        refresh_threshold=5,
        refresh_spy=spy,
    )

    for i in range(5):
        orch.handle_message(f"thought number {i}")

    assert len(spy) == 1, f"expected exactly one refresh, got {len(spy)}"


def test_refresh_writes_audit_entry_with_op_index_refresh(
    tmp_path: Path, lock_path: Path
) -> None:
    """The orchestrator audit-logs the refresh as op=index_refresh with duration_ms."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    audit_root = vault_root / "_audit"
    _seed_journal_domain(domains_root)

    orch = _build_orchestrator(
        vault_root=vault_root,
        domains_root=domains_root,
        audit_root=audit_root,
        lock_path=lock_path,
        refresh_threshold=5,
    )

    for i in range(5):
        orch.handle_message(f"thought number {i}")

    refresh_entries = [
        e for e in _audit_entries(audit_root) if e["op"] == "index_refresh"
    ]
    assert len(refresh_entries) == 1
    entry = refresh_entries[0]
    assert entry["actor"] == "kernel.orchestrator"
    assert entry["outcome"] == "ok"
    assert "duration_ms" in entry
    assert isinstance(entry["duration_ms"], int)


def test_counter_resets_after_refresh_so_next_five_trigger_again(
    tmp_path: Path, lock_path: Path
) -> None:
    """After a refresh, the next 5 writes trigger another refresh — not the 1st write."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    audit_root = vault_root / "_audit"
    _seed_journal_domain(domains_root)

    spy: list = []
    orch = _build_orchestrator(
        vault_root=vault_root,
        domains_root=domains_root,
        audit_root=audit_root,
        lock_path=lock_path,
        refresh_threshold=5,
        refresh_spy=spy,
    )

    # First 5 writes -> refresh #1
    for i in range(5):
        orch.handle_message(f"first batch {i}")
    assert len(spy) == 1

    # 6th write should NOT trigger (counter just reset to 0; now at 1).
    orch.handle_message("six")
    assert len(spy) == 1, "counter did not reset after refresh"

    # 3 more writes -> counter at 4, still below threshold.
    for i in range(3):
        orch.handle_message(f"second batch {i}")
    assert len(spy) == 1

    # The 5th post-reset write -> refresh #2.
    orch.handle_message("trigger")
    assert len(spy) == 2


def test_refresh_state_is_persisted_across_orchestrator_instances(
    tmp_path: Path, lock_path: Path
) -> None:
    """A new Orchestrator instance picks up the existing counter from disk."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    audit_root = vault_root / "_audit"
    _seed_journal_domain(domains_root)

    spy_a: list = []
    orch_a = _build_orchestrator(
        vault_root=vault_root,
        domains_root=domains_root,
        audit_root=audit_root,
        lock_path=lock_path,
        refresh_threshold=5,
        refresh_spy=spy_a,
    )
    for i in range(3):
        orch_a.handle_message(f"a{i}")
    orch_a.stop()

    # New orchestrator instance — must continue counting from 3, not from 0.
    spy_b: list = []
    orch_b = _build_orchestrator(
        vault_root=vault_root,
        domains_root=domains_root,
        audit_root=audit_root,
        lock_path=lock_path,
        refresh_threshold=5,
        refresh_spy=spy_b,
    )
    # 2 more writes -> hits threshold of 5.
    for i in range(2):
        orch_b.handle_message(f"b{i}")

    assert len(spy_b) == 1


def test_only_writes_increment_counter_not_classify_or_echo(
    tmp_path: Path, lock_path: Path
) -> None:
    """Echo / unknown-intent fallback turns must NOT increment the counter."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    audit_root = vault_root / "_audit"
    _seed_journal_domain(domains_root)

    spy: list = []
    # Classifier returns an intent the kernel does NOT dispatch as a write,
    # so handle_message goes through the echo path -> no vault write.
    classifier = _build_classifier(domains_root, "something.weird")

    config = {"context_engineering": {"index_refresh_after_writes": 5}}

    from kernel.index import refresh as real_refresh

    def _spy(*args, **kwargs):
        result = real_refresh(*args, **kwargs)
        spy.append(result)
        return result

    orch = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        config=config,
        index_refresh=_spy,
    )

    for i in range(10):
        orch.handle_message(f"weird {i}")

    # Echo path: zero writes counted, zero refreshes.
    assert spy == []
