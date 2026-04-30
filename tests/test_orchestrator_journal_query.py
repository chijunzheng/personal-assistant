"""Integration: orchestrator wires retrieval -> journal.read -> audit -> reply.

These tests verify the new ``journal.query`` dispatch in
``Orchestrator.handle_message``. With a temp vault pre-seeded with INDEX +
session + a few journal notes, a "what did I think about X?" message must:

  - Call ``kernel.retrieval.gather_context`` with the query
  - Call ``domains.journal.handler.read`` with the resulting bundle
  - Audit-log a ``read`` op carrying ``domain="journal"``, ``intent``, and
    a list of every consulted path
  - Reply with the LLM-produced text

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


def _stub_invoker(text: str = "stubbed", tokens_in: int = 1, tokens_out: int = 1):
    """Build a minimal ``claude_runner.invoke``-shaped stub."""

    last: dict = {}

    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        last["prompt"] = prompt
        last["system_prompt"] = system_prompt
        return ClaudeResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    _invoke.last = last  # type: ignore[attr-defined]
    return _invoke


def _seed_journal_domain(domains_root: Path) -> None:
    """Drop a journal ``domain.yaml`` so the classifier knows the intent label."""
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


def _seed_vault(vault_root: Path) -> dict[str, Path]:
    """Populate the vault with INDEX, session, and a couple of journal notes."""
    (vault_root / "_index").mkdir(parents=True, exist_ok=True)
    (vault_root / "journal").mkdir(parents=True, exist_ok=True)

    index = vault_root / "_index" / "INDEX.md"
    index.write_text("# INDEX\n\nconsciousness, qualia, agents\n", encoding="utf-8")

    session = vault_root / "_index" / "active_session.md"
    session.write_text(
        "---\n"
        "chat_id: test-chat\n"
        "session_id: prior\n"
        "started_at: 2026-04-29T11:00:00+00:00\n"
        "last_updated: 2026-04-29T11:30:00+00:00\n"
        "turns: 1\n"
        "---\n\n"
        "- earlier: thinking about consciousness\n",
        encoding="utf-8",
    )

    note_a = vault_root / "journal" / "2026-04-15-consciousness.md"
    note_a.write_text("today I read about consciousness and qualia\n", encoding="utf-8")

    note_b = vault_root / "journal" / "2026-04-16-grocery.md"
    note_b.write_text("went to the grocery store\n", encoding="utf-8")

    return {"index": index, "session": session, "note_a": note_a, "note_b": note_b}


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def test_journal_query_invokes_retrieval_and_replies(
    tmp_path: Path, lock_path: Path
) -> None:
    """End-to-end: query -> retrieval -> read -> reply text from runner."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    seeded = _seed_vault(vault_root)

    classifier = Classifier(
        domains_root=domains_root,
        invoker=_stub_invoker(text="journal.query"),
        prompt_template="",
    )
    runner = _stub_invoker(text="You wrote about consciousness on 2026-04-15.")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=runner,
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    reply = orchestrator.handle_message("what did I think about consciousness?")

    assert reply.text == "You wrote about consciousness on 2026-04-15."
    # The journal note that matched should appear in the prompt the runner saw.
    assert "consciousness" in runner.last["prompt"]
    assert seeded["note_a"].name in runner.last["prompt"]


def test_journal_query_audit_records_read_with_consulted_paths(
    tmp_path: Path, lock_path: Path
) -> None:
    """The audit log gains a read entry whose ``paths`` lists every consulted file."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    seeded = _seed_vault(vault_root)

    classifier = Classifier(
        domains_root=domains_root,
        invoker=_stub_invoker(text="journal.query"),
        prompt_template="",
    )

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(text="answered"),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    orchestrator.handle_message("what did I think about consciousness?")

    audit_files = list((vault_root / "_audit").glob("*.jsonl"))
    assert len(audit_files) == 1
    entries = [
        json.loads(line)
        for line in audit_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    read_entries = [e for e in entries if e["op"] == "read"]
    assert len(read_entries) == 1

    record = read_entries[0]
    assert record["domain"] == "journal"
    assert record["intent"] == "journal.query"
    assert record["outcome"] == "ok"
    paths = record.get("paths") or []
    # Every consulted file should appear in the audit's paths list.
    assert any(str(seeded["index"]) == p for p in paths)
    assert any(str(seeded["note_a"]) == p for p in paths)
    # The unrelated grocery note must NOT appear (grep filtered it out).
    assert not any(str(seeded["note_b"]) == p for p in paths)


def test_journal_query_reply_when_runner_fails_is_friendly(
    tmp_path: Path, lock_path: Path
) -> None:
    """Runner errors during read still produce a user-facing reply, audit-logged with error."""
    from kernel.claude_runner import ClaudeRunnerError

    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    _seed_vault(vault_root)

    classifier = Classifier(
        domains_root=domains_root,
        invoker=_stub_invoker(text="journal.query"),
        prompt_template="",
    )

    def _failing_invoker(prompt, *, system_prompt: Optional[str] = None):
        raise ClaudeRunnerError("boom")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_failing_invoker,
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    reply = orchestrator.handle_message("what did I think about consciousness?")

    assert "wrong" in reply.text.lower() or "sorry" in reply.text.lower()

    audit_files = list((vault_root / "_audit").glob("*.jsonl"))
    entries = [
        json.loads(line)
        for line in audit_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    read_entries = [e for e in entries if e["op"] == "read"]
    assert len(read_entries) == 1
    assert read_entries[0]["outcome"] == "error"
    assert "boom" in read_entries[0]["error"]
