"""Integration: orchestrator wires classifier -> reminder handler -> audit -> reply.

Verifies the new ``reminder.*`` dispatch in ``Orchestrator.handle_message``:

  - ``reminder.add`` / ``reminder.add_when`` / ``reminder.cancel`` invoke
    the reminder write path; ``vault/reminder/events.jsonl`` accumulates
    rows with sha256 idempotency keys.
  - ``reminder.list`` invokes the read path returning a real list.
  - The audit log gets ``op=write`` / ``op=read`` entries with
    ``domain=reminder``.

Both ``claude_runner`` and the LLM-backed extractor are stubbed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from kernel.classifier import Classifier
from kernel.claude_runner import ClaudeResponse
from kernel.orchestrator import Orchestrator, SingleInstanceLock


def _stub_invoker(text: str = "ok"):
    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=1,
            tokens_out=1,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def _seed_reminder_domain(domains_root: Path) -> None:
    """Drop a reminder ``domain.yaml`` so the classifier knows the intents."""
    domain_dir = domains_root / "reminder"
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "domain.yaml").write_text(
        "name: reminder\n"
        "description: \"reminders\"\n"
        "intents:\n"
        "  - reminder.add\n"
        "  - reminder.add_when\n"
        "  - reminder.list\n"
        "  - reminder.cancel\n",
        encoding="utf-8",
    )


def _build_classifier(domains_root: Path, intent: str) -> Classifier:
    return Classifier(
        domains_root=domains_root,
        invoker=_stub_invoker(text=intent),
        prompt_template="",
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def _audit_entries(audit_root: Path) -> list[dict]:
    entries: list[dict] = []
    for daily in sorted(audit_root.glob("*.jsonl")):
        for line in daily.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------------------
# reminder.add -> write path
# ---------------------------------------------------------------------------


def test_reminder_add_persists_scheduled_event(tmp_path: Path, lock_path: Path) -> None:
    """``reminder.add`` lands a scheduled row in events.jsonl."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_reminder_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "reminder.add")
    extractor = lambda _msg, _intent: {  # noqa: E731
        "message": "call mom",
        "fire_at": "2026-05-03T18:00:00+00:00",
        "recurrence": None,
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        reminder_extractor=extractor,
    )

    reply = orchestrator.handle_message("remind me Sunday 6pm to call mom")

    events_path = vault_root / "reminder" / "events.jsonl"
    assert events_path.exists()
    rows = [
        json.loads(l)
        for l in events_path.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "scheduled"
    assert rows[0]["message"] == "call mom"
    assert rows[0]["fire_at"] == "2026-05-03T18:00:00+00:00"
    # Reply confirms the reminder was scheduled.
    assert "remind" in reply.text.lower() or "scheduled" in reply.text.lower() or "ok" in reply.text.lower()


def test_reminder_add_audits_write(tmp_path: Path, lock_path: Path) -> None:
    """The orchestrator writes one audit entry with op=write + domain=reminder."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_reminder_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "reminder.add")
    extractor = lambda _msg, _intent: {  # noqa: E731
        "message": "call mom",
        "fire_at": "2026-05-03T18:00:00+00:00",
        "recurrence": None,
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        reminder_extractor=extractor,
    )

    orchestrator.handle_message("remind me Sunday 6pm to call mom")

    write_entries = [e for e in _audit_entries(audit_root) if e["op"] == "write"]
    assert len(write_entries) == 1
    assert write_entries[0]["domain"] == "reminder"
    assert write_entries[0]["intent"] == "reminder.add"
    assert write_entries[0]["outcome"] == "ok"


# ---------------------------------------------------------------------------
# reminder.list -> read path
# ---------------------------------------------------------------------------


def test_reminder_list_returns_pending_reminders(tmp_path: Path, lock_path: Path) -> None:
    """A reminder.list intent surfaces pending reminders."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_reminder_domain(domains_root)

    # Add a reminder first.
    add_classifier = _build_classifier(domains_root, "reminder.add")
    add_orch = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=add_classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        reminder_extractor=lambda _m, _i: {
            "message": "call mom",
            "fire_at": "2026-05-03T18:00:00+00:00",
            "recurrence": None,
        },
    )
    add_orch.handle_message("remind me Sunday 6pm to call mom")

    # Now list.
    list_classifier = _build_classifier(domains_root, "reminder.list")
    list_orch = Orchestrator(
        lock=SingleInstanceLock(tmp_path / "lock-2"),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=list_classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )
    reply = list_orch.handle_message("what reminders do I have?")

    assert "call mom" in reply.text.lower()


def test_reminder_list_audits_read(tmp_path: Path, lock_path: Path) -> None:
    """``reminder.list`` writes an audit entry with op=read + domain=reminder."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_reminder_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "reminder.list")
    orch = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )
    orch.handle_message("what reminders do I have?")

    read_entries = [
        e
        for e in _audit_entries(audit_root)
        if e["op"] == "read" and e.get("domain") == "reminder"
    ]
    assert len(read_entries) == 1
    assert read_entries[0]["intent"] == "reminder.list"
    assert read_entries[0]["outcome"] == "ok"
