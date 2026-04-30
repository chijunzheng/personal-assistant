"""Tests for ``kernel.proactive`` — daily/weekly digest assembly + reminder dispatch.

The proactive entrypoint is a CLI dispatcher (argparse) with three
subcommands: ``daily-digest``, ``weekly-digest``, ``check-reminders``.
The pure-function helpers are exercised directly so we don't shell out
to a subprocess in unit tests.

Each domain's ``digest.py`` is auto-discovered from
``domains/<name>/domain.yaml`` (the ``digest:`` block). The proactive
layer NEVER hardcodes a domain name — adding a new domain with a digest
is purely a YAML + plugin change.

The advisory pass (``suggested_actions=true``) shells to ``claude_runner``;
both the pass and the reminder dispatcher use a pluggable callable so
tests inject deterministic stubs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from kernel.claude_runner import ClaudeResponse
from kernel.proactive import (
    check_reminders,
    daily_digest,
    weekly_digest,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_domain_yaml(domains_root: Path, name: str, body: str) -> None:
    """Drop a single domain.yaml so ``daily_digest`` can discover it."""
    target = domains_root / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "domain.yaml").write_text(body, encoding="utf-8")


def _seed_inventory_state(vault_root: Path, state: dict) -> None:
    inventory = vault_root / "inventory"
    inventory.mkdir(parents=True, exist_ok=True)
    (inventory / "state.yaml").write_text(
        yaml.safe_dump(state, sort_keys=True), encoding="utf-8"
    )


def _seed_fitness(vault_root: Path) -> None:
    fitness = vault_root / "fitness"
    fitness.mkdir(parents=True, exist_ok=True)
    (fitness / "profile.yaml").write_text(
        yaml.safe_dump(
            {"target_calories_kcal": 2200, "target_protein_g": 160, "weekly_training_days": 4},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    today = "2026-04-29"
    (fitness / "meals.jsonl").write_text(
        json.dumps(
            {
                "id": "m1",
                "ts": f"{today}T08:30:00+00:00",
                "meal_type": "breakfast",
                "items": [],
                "total_kcal": 600,
                "total_protein_g": 40,
                "total_carbs_g": 60,
                "total_fat_g": 20,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _seed_journal(vault_root: Path) -> None:
    journal = vault_root / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    (journal / "2026-04-29-rag.md").write_text(
        "---\n"
        "date: 2026-04-29T08:00:00+00:00\n"
        "tags: [rag]\n"
        "links: []\n"
        "source: telegram\n"
        "session_id: t\n"
        "---\n\n"
        "RAG thoughts\n",
        encoding="utf-8",
    )


def _seed_finance(vault_root: Path) -> None:
    finance = vault_root / "finance"
    finance.mkdir(parents=True, exist_ok=True)
    (finance / "transactions.jsonl").write_text(
        json.dumps(
            {
                "id": "t1",
                "date": "2026-04-25",
                "amount": -4.50,
                "merchant": "Cafe",
                "category": "coffee",
                "currency": "CAD",
                "raw": "x",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _all_domains(domains_root: Path) -> None:
    """Seed all four daily/weekly-relevant domains for digest tests."""
    _seed_domain_yaml(
        domains_root,
        "inventory",
        "name: inventory\n"
        "intents: [inventory.add]\n"
        "digest:\n"
        "  enabled: true\n"
        "  cadence: daily\n"
        "  module: digest.py:summarize\n",
    )
    _seed_domain_yaml(
        domains_root,
        "fitness",
        "name: fitness\n"
        "intents: [fitness.workout_log]\n"
        "digest:\n"
        "  enabled: true\n"
        "  daily:\n"
        "    enabled: true\n"
        "  weekly:\n"
        "    enabled: true\n"
        "  module: digest.py:summarize\n",
    )
    _seed_domain_yaml(
        domains_root,
        "journal",
        "name: journal\n"
        "intents: [journal.capture]\n"
        "digest:\n"
        "  enabled: true\n"
        "  cadence: weekly\n"
        "  module: digest.py:summarize\n",
    )
    _seed_domain_yaml(
        domains_root,
        "finance",
        "name: finance\n"
        "intents: [finance.transaction]\n"
        "digest:\n"
        "  enabled: true\n"
        "  cadence: weekly\n"
        "  module: digest.py:summarize\n",
    )
    _seed_domain_yaml(
        domains_root,
        "reminder",
        "name: reminder\n"
        "intents: [reminder.add]\n"
        "digest:\n"
        "  enabled: false\n",
    )


def _stub_invoker_returning(text: str):
    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=10,
            tokens_out=20,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def _fixed_now() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# daily-digest
# ---------------------------------------------------------------------------


def test_daily_digest_composes_from_each_enabled_daily_domain(tmp_path: Path) -> None:
    """The daily digest text concatenates every domain whose digest cadence == daily."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _all_domains(domains_root)
    _seed_inventory_state(
        vault_root,
        {"milk": {"quantity": 0, "unit": "count", "low_threshold": 1}},
    )
    _seed_fitness(vault_root)

    text = daily_digest(
        vault_root=vault_root,
        domains_root=domains_root,
        config={"context_engineering": {"suggested_actions": False}},
        now=_fixed_now(),
    )

    # Inventory contributes (low-stock).
    assert "milk" in text.lower()
    # Fitness contributes (calorie/protein pacing for today).
    assert "calories" in text.lower() or "protein" in text.lower()
    # Weekly-only domains do NOT appear in the daily digest.
    assert "topics this week" not in text.lower()
    assert "spending this week" not in text.lower()


def test_daily_digest_returns_empty_when_no_enabled_daily_domains(
    tmp_path: Path,
) -> None:
    """No domains with daily cadence => empty string."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_domain_yaml(
        domains_root,
        "journal",
        "name: journal\n"
        "intents: [journal.capture]\n"
        "digest:\n  enabled: true\n  cadence: weekly\n  module: digest.py:summarize\n",
    )

    text = daily_digest(
        vault_root=vault_root,
        domains_root=domains_root,
        config={},
        now=_fixed_now(),
    )
    assert text.strip() == ""


def test_daily_digest_invokes_advisory_pass_when_suggested_actions_on(
    tmp_path: Path,
) -> None:
    """``suggested_actions=true`` triggers a single ``claude_runner`` call."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _all_domains(domains_root)
    _seed_inventory_state(
        vault_root,
        {"milk": {"quantity": 0, "unit": "count", "low_threshold": 1}},
    )
    _seed_fitness(vault_root)

    invocations: list[str] = []

    def _invoker(prompt, *, system_prompt: Optional[str] = None):
        invocations.append(prompt)
        return ClaudeResponse(
            text="### Suggested actions\n- Pick up milk on the way home.",
            tokens_in=10,
            tokens_out=20,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    text = daily_digest(
        vault_root=vault_root,
        domains_root=domains_root,
        config={"context_engineering": {"suggested_actions": True}},
        now=_fixed_now(),
        invoker=_invoker,
    )

    assert len(invocations) == 1
    assert "Suggested actions" in text


def test_daily_digest_skips_advisory_when_flag_off(tmp_path: Path) -> None:
    """``suggested_actions=false`` => no claude_runner call."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _all_domains(domains_root)
    _seed_inventory_state(
        vault_root,
        {"milk": {"quantity": 0, "unit": "count", "low_threshold": 1}},
    )

    invocations: list[str] = []

    def _invoker(prompt, *, system_prompt: Optional[str] = None):
        invocations.append(prompt)
        return ClaudeResponse(text="should not be called", tokens_in=0, tokens_out=0, raw={})

    daily_digest(
        vault_root=vault_root,
        domains_root=domains_root,
        config={"context_engineering": {"suggested_actions": False}},
        now=_fixed_now(),
        invoker=_invoker,
    )
    assert invocations == []


# ---------------------------------------------------------------------------
# weekly-digest
# ---------------------------------------------------------------------------


def test_weekly_digest_composes_from_each_enabled_weekly_domain(
    tmp_path: Path,
) -> None:
    """Weekly digest concatenates each domain whose digest cadence == weekly."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _all_domains(domains_root)
    _seed_journal(vault_root)
    _seed_finance(vault_root)
    _seed_fitness(vault_root)

    text = weekly_digest(
        vault_root=vault_root,
        domains_root=domains_root,
        config={"context_engineering": {"suggested_actions": False}},
        now=_fixed_now(),
    )

    # Journal weekly section.
    assert "topics this week" in text.lower() or "rag" in text.lower()
    # Finance weekly section.
    assert "spending" in text.lower() or "coffee" in text.lower()
    # Fitness weekly contributes too (workouts/calories).
    # Daily-only inventory should NOT appear.
    assert "running low" not in text.lower()


def test_weekly_digest_includes_inbox_triage_section(tmp_path: Path) -> None:
    """Weekly digest enumerates files under vault/_inbox/ older than the threshold."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _all_domains(domains_root)

    # Seed an inbox file (top-level, not in _conflicts/_pending_edits).
    inbox = vault_root / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "ambiguous-1.md").write_text("a thought I wasn't sure how to classify", encoding="utf-8")

    text = weekly_digest(
        vault_root=vault_root,
        domains_root=domains_root,
        config={"context_engineering": {"suggested_actions": False}},
        now=_fixed_now(),
    )

    assert "inbox" in text.lower()
    assert "ambiguous-1" in text


def test_weekly_digest_inbox_excludes_conflicts_and_pending_edits(
    tmp_path: Path,
) -> None:
    """``_conflicts`` and ``_pending_edits`` subdirs do not surface in inbox triage."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _all_domains(domains_root)

    inbox = vault_root / "_inbox"
    (inbox / "_conflicts").mkdir(parents=True, exist_ok=True)
    (inbox / "_conflicts" / "conflict.md").write_text("c", encoding="utf-8")
    (inbox / "_pending_edits").mkdir(parents=True, exist_ok=True)
    (inbox / "_pending_edits" / "pending.md").write_text("p", encoding="utf-8")

    text = weekly_digest(
        vault_root=vault_root,
        domains_root=domains_root,
        config={"context_engineering": {"suggested_actions": False}},
        now=_fixed_now(),
    )

    assert "conflict.md" not in text
    assert "pending.md" not in text


# ---------------------------------------------------------------------------
# check-reminders
# ---------------------------------------------------------------------------


def test_check_reminders_fires_due_scheduled_reminders(tmp_path: Path) -> None:
    """Past-due scheduled reminders are sent via the telegram hook."""
    vault_root = tmp_path / "vault"
    audit_root = vault_root / "_audit"

    # Seed a past-due scheduled reminder by appending directly.
    rem_dir = vault_root / "reminder"
    rem_dir.mkdir(parents=True)
    row = {
        "id": "abc123" * 10,  # 60 chars; not real sha256 but ok
        "kind": "scheduled",
        "status": "pending",
        "message": "call mom",
        "fire_at": "2026-04-28T12:00:00+00:00",
        "recurrence": None,
        "created_at": "2026-04-25T10:00:00+00:00",
        "source": "telegram",
        "session_id": "s",
    }
    (rem_dir / "events.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    sent: list[str] = []

    def _telegram_send(message: str) -> None:
        sent.append(message)

    fired = check_reminders(
        vault_root=vault_root,
        audit_root=audit_root,
        now=_fixed_now(),
        telegram_send=_telegram_send,
    )

    assert len(fired) == 1
    assert "call mom" in sent[0]


def test_check_reminders_appends_fire_event_to_jsonl(tmp_path: Path) -> None:
    """Firing a reminder appends a new event row (preserves append-only)."""
    vault_root = tmp_path / "vault"
    audit_root = vault_root / "_audit"
    rem_dir = vault_root / "reminder"
    rem_dir.mkdir(parents=True)
    row = {
        "id": "abc123" * 10,
        "kind": "scheduled",
        "status": "pending",
        "message": "call mom",
        "fire_at": "2026-04-28T12:00:00+00:00",
        "recurrence": None,
        "created_at": "2026-04-25T10:00:00+00:00",
        "source": "telegram",
        "session_id": "s",
    }
    (rem_dir / "events.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    check_reminders(
        vault_root=vault_root,
        audit_root=audit_root,
        now=_fixed_now(),
        telegram_send=lambda _m: None,
    )

    lines = [
        json.loads(l)
        for l in (rem_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    # Original row + one fire event.
    assert len(lines) == 2
    assert lines[0]["status"] == "pending"  # original UNCHANGED
    assert lines[1]["status"] == "fired"
    assert lines[1]["target_id"] == row["id"]


def test_check_reminders_audit_logs_fire(tmp_path: Path) -> None:
    """Firing a reminder writes an audit entry with op=reminder_fire."""
    vault_root = tmp_path / "vault"
    audit_root = vault_root / "_audit"
    rem_dir = vault_root / "reminder"
    rem_dir.mkdir(parents=True)
    row = {
        "id": "abc123" * 10,
        "kind": "scheduled",
        "status": "pending",
        "message": "call mom",
        "fire_at": "2026-04-28T12:00:00+00:00",
        "recurrence": None,
        "created_at": "2026-04-25T10:00:00+00:00",
        "source": "telegram",
        "session_id": "s",
    }
    (rem_dir / "events.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    check_reminders(
        vault_root=vault_root,
        audit_root=audit_root,
        now=_fixed_now(),
        telegram_send=lambda _m: None,
    )

    audit_files = list(audit_root.glob("*.jsonl"))
    assert audit_files, "audit log file should be created"
    entries = []
    for f in audit_files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    fire_entries = [e for e in entries if e["op"] == "reminder_fire"]
    assert len(fire_entries) == 1
    assert fire_entries[0]["actor"] == "kernel.proactive"
    assert fire_entries[0]["outcome"] == "ok"


def test_check_reminders_skips_future_reminders(tmp_path: Path) -> None:
    """Future-dated reminders are not fired."""
    vault_root = tmp_path / "vault"
    audit_root = vault_root / "_audit"
    rem_dir = vault_root / "reminder"
    rem_dir.mkdir(parents=True)
    row = {
        "id": "future" * 10,
        "kind": "scheduled",
        "status": "pending",
        "message": "future thing",
        "fire_at": "2026-12-31T12:00:00+00:00",
        "recurrence": None,
        "created_at": "2026-04-25T10:00:00+00:00",
        "source": "telegram",
        "session_id": "s",
    }
    (rem_dir / "events.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    sent: list[str] = []
    fired = check_reminders(
        vault_root=vault_root,
        audit_root=audit_root,
        now=_fixed_now(),
        telegram_send=lambda m: sent.append(m),
    )
    assert fired == []
    assert sent == []


def test_check_reminders_returns_empty_when_no_reminders(tmp_path: Path) -> None:
    """Missing events.jsonl is non-fatal — returns []."""
    vault_root = tmp_path / "vault"
    audit_root = vault_root / "_audit"
    fired = check_reminders(
        vault_root=vault_root,
        audit_root=audit_root,
        now=_fixed_now(),
        telegram_send=lambda _m: None,
    )
    assert fired == []
