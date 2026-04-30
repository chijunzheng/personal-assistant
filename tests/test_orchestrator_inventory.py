"""Integration: orchestrator wires classifier -> inventory handler -> audit -> reply.

Verifies the new ``inventory.*`` dispatch in ``Orchestrator.handle_message``:

  - ``inventory.add | inventory.consume | inventory.adjust`` invoke the
    inventory write path; the events JSONL appears under
    ``vault/inventory/events.jsonl`` and ``state.yaml`` is recomputed; an
    audit entry with ``op=write`` + ``domain=inventory`` is appended.
  - Re-sending the same natural-language event does not duplicate rows.
  - ``inventory.query`` / ``inventory.list_low`` invoke the read path; the
    audit log gets a ``op=read`` entry and the reply text contains a
    real number / item list.

Both ``claude_runner`` and the LLM-backed extractor are stubbed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from kernel.classifier import Classifier
from kernel.claude_runner import ClaudeResponse
from kernel.orchestrator import Orchestrator, SingleInstanceLock


def _stub_invoker(text: str = "ok", tokens_in: int = 1, tokens_out: int = 1):
    """A claude_runner.invoke-shaped stub."""

    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def _seed_inventory_domain(domains_root: Path) -> None:
    """Drop an inventory ``domain.yaml`` so the classifier knows the intents."""
    domain_dir = domains_root / "inventory"
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "domain.yaml").write_text(
        "name: inventory\n"
        "description: \"household items\"\n"
        "intents:\n"
        "  - inventory.add\n"
        "  - inventory.consume\n"
        "  - inventory.adjust\n"
        "  - inventory.query\n"
        "  - inventory.list_low\n",
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
# inventory.add -> write path
# ---------------------------------------------------------------------------


def test_inventory_add_appends_event_and_recomputes_state(
    tmp_path: Path, lock_path: Path
) -> None:
    """A inventory.add intent persists an event row and a state.yaml entry."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_inventory_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "inventory.add")

    extractor = lambda _msg, _intent: {  # noqa: E731
        "item": "milk",
        "quantity_delta": 2,
        "unit": "count",
        "location": "fridge",
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        inventory_extractor=extractor,
    )

    reply = orchestrator.handle_message("bought 2 milks at Costco")

    events_path = vault_root / "inventory" / "events.jsonl"
    state_path = vault_root / "inventory" / "state.yaml"
    assert events_path.exists()
    assert state_path.exists()
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["item"] == "milk"
    assert rows[0]["quantity_delta"] == 2
    assert rows[0]["type"] == "bought"

    state = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
    assert state["milk"]["quantity"] == 2

    # Reply confirms the operation.
    assert "milk" in reply.text.lower()


def test_inventory_add_audit_records_write(tmp_path: Path, lock_path: Path) -> None:
    """The orchestrator writes one audit entry with op=write + domain=inventory."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_inventory_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "inventory.add")
    extractor = lambda _msg, _intent: {"item": "milk", "quantity_delta": 2}  # noqa: E731

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        inventory_extractor=extractor,
    )

    orchestrator.handle_message("bought 2 milks")

    write_entries = [e for e in _audit_entries(audit_root) if e["op"] == "write"]
    assert len(write_entries) == 1
    assert write_entries[0]["domain"] == "inventory"
    assert write_entries[0]["intent"] == "inventory.add"
    assert write_entries[0]["outcome"] == "ok"
    assert "events.jsonl" in write_entries[0]["path"]


def test_inventory_add_is_idempotent_in_orchestrator(
    tmp_path: Path, lock_path: Path
) -> None:
    """Re-issuing the same natural-language message does not duplicate the row."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_inventory_domain(domains_root)

    classifier = _build_classifier(domains_root, "inventory.add")
    extractor = lambda _msg, _intent: {"item": "milk", "quantity_delta": 2}  # noqa: E731

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        inventory_extractor=extractor,
    )

    orchestrator.handle_message("bought 2 milks at Costco")
    orchestrator.handle_message("bought 2 milks at Costco")

    events_path = vault_root / "inventory" / "events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    state = yaml.safe_load((vault_root / "inventory" / "state.yaml").read_text())
    assert state["milk"]["quantity"] == 2


def test_inventory_consume_lowers_state(tmp_path: Path, lock_path: Path) -> None:
    """After buying then consuming, state shows the difference."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_inventory_domain(domains_root)

    # First a buy.
    add_classifier = _build_classifier(domains_root, "inventory.add")
    add_orch = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=add_classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        inventory_extractor=lambda _m, _i: {"item": "milk", "quantity_delta": 5},
    )
    add_orch.handle_message("bought 5 milks")

    # Then a consume.
    consume_classifier = _build_classifier(domains_root, "inventory.consume")
    consume_orch = Orchestrator(
        lock=SingleInstanceLock(tmp_path / "lock-2"),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=consume_classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        inventory_extractor=lambda _m, _i: {"item": "milk", "quantity_delta": 2},
    )
    consume_orch.handle_message("used 2 milks for cereal")

    state = yaml.safe_load((vault_root / "inventory" / "state.yaml").read_text())
    assert state["milk"]["quantity"] == 3


# ---------------------------------------------------------------------------
# inventory.list_low -> read path
# ---------------------------------------------------------------------------


def test_inventory_list_low_returns_real_running_low_items(
    tmp_path: Path, lock_path: Path
) -> None:
    """inventory.list_low reply lists the actually-low items from state.yaml."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_inventory_domain(domains_root)

    # Seed state directly with a low + a not-low item.
    inventory = vault_root / "inventory"
    inventory.mkdir(parents=True, exist_ok=True)
    (inventory / "state.yaml").write_text(
        yaml.safe_dump(
            {
                "milk": {"quantity": 0, "unit": "count", "low_threshold": 1},
                "eggs": {"quantity": 12, "unit": "count", "low_threshold": 6},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    classifier = _build_classifier(domains_root, "inventory.list_low")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    reply = orchestrator.handle_message("what's running low?")
    assert "milk" in reply.text.lower()
    assert "eggs" not in reply.text.lower()


def test_inventory_query_returns_real_quantity(
    tmp_path: Path, lock_path: Path
) -> None:
    """inventory.query reply mentions the queried item + its real quantity."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_inventory_domain(domains_root)

    inventory = vault_root / "inventory"
    inventory.mkdir(parents=True, exist_ok=True)
    (inventory / "state.yaml").write_text(
        yaml.safe_dump(
            {"AAA batteries": {"quantity": 4, "unit": "count", "low_threshold": 2}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    classifier = _build_classifier(domains_root, "inventory.query")
    parser = lambda _q: {"mode": "item", "item": "AAA batteries"}  # noqa: E731

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        inventory_query_parser=parser,
    )

    reply = orchestrator.handle_message("do I still have AAA batteries?")
    assert "4" in reply.text
    assert "aaa" in reply.text.lower() or "batteries" in reply.text.lower()


def test_inventory_query_audit_records_read(
    tmp_path: Path, lock_path: Path
) -> None:
    """inventory.query produces a read audit entry with domain=inventory."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_inventory_domain(domains_root)

    inventory = vault_root / "inventory"
    inventory.mkdir(parents=True, exist_ok=True)
    (inventory / "state.yaml").write_text(
        yaml.safe_dump({"milk": {"quantity": 3, "unit": "count", "low_threshold": 1}}),
        encoding="utf-8",
    )

    classifier = _build_classifier(domains_root, "inventory.query")
    parser = lambda _q: {"mode": "item", "item": "milk"}  # noqa: E731

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        inventory_query_parser=parser,
    )
    orchestrator.handle_message("do I have milk?")

    read_entries = [
        e for e in _audit_entries(vault_root / "_audit") if e["op"] == "read"
    ]
    assert len(read_entries) == 1
    assert read_entries[0]["domain"] == "inventory"
    assert read_entries[0]["intent"] == "inventory.query"
    assert read_entries[0]["outcome"] == "ok"


def test_inventory_unrelated_intents_dont_route_to_inventory(
    tmp_path: Path, lock_path: Path
) -> None:
    """Journal intents must not write to ``vault/inventory/`` — plugin isolation."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_inventory_domain(domains_root)

    journal_dir = domains_root / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "domain.yaml").write_text(
        "name: journal\nintents:\n  - journal.capture\n", encoding="utf-8"
    )

    classifier = _build_classifier(domains_root, "journal.capture")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )
    orchestrator.handle_message("a thought")
    assert not (vault_root / "inventory" / "events.jsonl").exists()
    assert not (vault_root / "inventory" / "state.yaml").exists()
