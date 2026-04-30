"""Integration: orchestrator wires classifier -> journal handler -> audit -> reply.

These tests verify the new journal-write path in ``Orchestrator.handle_message``:

  - A clear ``journal.capture`` intent dispatches to the journal handler
  - The handler write produces a file under ``vault/journal/``
  - The audit log gains a ``write`` entry with ``domain=journal`` and the
    written path
  - The reply text confirms the file was saved
  - An unknown intent falls through to the existing echo path (issue #1
    behavior preserved as the fallback for unrecognized intents)

Both ``claude_runner`` and ``Telegram`` are mocked.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from kernel.classifier import Classifier
from kernel.claude_runner import ClaudeResponse
from kernel.orchestrator import Orchestrator, SingleInstanceLock


def _stub_invoker(text: str = "echo reply", tokens_in: int = 1, tokens_out: int = 1):
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
    """Drop a journal ``domain.yaml`` so the classifier has a real intent to return."""
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


def test_journal_capture_writes_file_in_vault_journal_dir(
    tmp_path: Path, lock_path: Path
) -> None:
    """A journal.capture intent persists a file under ``vault/journal/``."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "journal.capture")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    reply = orchestrator.handle_message("interesting idea about tiered memory")

    journal_files = list((vault_root / "journal").iterdir())
    assert len(journal_files) == 1
    assert journal_files[0].name.startswith("2026-04-29-")
    assert "tiered" in journal_files[0].name
    # Reply should confirm the path the kernel wrote to.
    assert "journal" in reply.text


def test_journal_capture_audit_entry_records_write(tmp_path: Path, lock_path: Path) -> None:
    """The orchestrator writes one audit entry with op=write and the path."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "journal.capture")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    orchestrator.handle_message("a thought worth keeping")

    daily_files = list(audit_root.glob("*.jsonl"))
    assert len(daily_files) == 1
    entries = [
        json.loads(line)
        for line in daily_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    write_entries = [e for e in entries if e["op"] == "write"]
    assert len(write_entries) == 1
    assert write_entries[0]["domain"] == "journal"
    assert write_entries[0]["intent"] == "journal.capture"
    assert "vault/journal/" in write_entries[0]["path"] or write_entries[0]["path"].endswith(".md")
    assert write_entries[0]["outcome"] == "ok"


def test_unknown_intent_falls_back_to_echo_path(tmp_path: Path, lock_path: Path) -> None:
    """An intent the kernel doesn't dispatch (e.g. inbox fallback) still gets a reply."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    audit_root = vault_root / "_audit"

    # Classifier emits a label not in the registry -> falls back to _inbox.fallback.
    classifier = Classifier(
        domains_root=domains_root,
        invoker=_stub_invoker(text="something.weird"),
        prompt_template="",
    )

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(text="acknowledged"),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    reply = orchestrator.handle_message("???")

    assert reply.text == "acknowledged"
    # No journal file was written.
    assert not (vault_root / "journal").exists() or not list((vault_root / "journal").iterdir())


def test_journal_capture_is_idempotent_in_orchestrator(
    tmp_path: Path, lock_path: Path
) -> None:
    """Re-sending the same message in the same session does not duplicate the file."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "journal.capture")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    orchestrator.handle_message("the same thought twice")
    orchestrator.handle_message("the same thought twice")

    journal_files = list((vault_root / "journal").iterdir())
    assert len(journal_files) == 1
