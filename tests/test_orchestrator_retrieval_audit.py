"""Issue #10 — orchestrator records bundle telemetry on read audit entries.

When the orchestrator gathers a context bundle for a journal query, the
resulting audit ``read`` entry must carry:

  - ``tokens_in_context_bundle``: the bundle's char-proxy token estimate
  - ``flags``: the eight engineering Booleans that were ON for this turn

These fields are what the eval harness aggregates to produce per-turn
charts of bundle weight + flag effects.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
import yaml

from kernel.classifier import Classifier
from kernel.claude_runner import ClaudeResponse
from kernel.orchestrator import Orchestrator, SingleInstanceLock


def _stub_invoker(text: str = "stubbed", tokens_in: int = 1, tokens_out: int = 1):
    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def _seed_journal_domain(domains_root: Path) -> None:
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


def _seed_vault(vault_root: Path) -> None:
    (vault_root / "_index").mkdir(parents=True, exist_ok=True)
    (vault_root / "journal").mkdir(parents=True, exist_ok=True)
    (vault_root / "_index" / "INDEX.md").write_text(
        "# INDEX\n\nconsciousness, qualia\n", encoding="utf-8"
    )
    (vault_root / "_index" / "active_session.md").write_text(
        "session: thinking\n", encoding="utf-8"
    )
    (vault_root / "journal" / "2026-04-15-consciousness.md").write_text(
        "thoughts on consciousness\n", encoding="utf-8"
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_config(name: str) -> dict:
    return yaml.safe_load(
        (_project_root() / "configs" / f"{name}.yaml").read_text(encoding="utf-8")
    ) or {}


def _orchestrator_for(
    *,
    config: dict,
    config_label: str,
    vault_root: Path,
    domains_root: Path,
    lock_path: Path,
) -> Orchestrator:
    classifier = Classifier(
        domains_root=domains_root,
        invoker=_stub_invoker(text="journal.query"),
        prompt_template="",
    )
    return Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(text="answered"),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        config=config,
        config_label=config_label,
    )


def _read_audit_entries(vault_root: Path) -> list[dict]:
    audit_files = list((vault_root / "_audit").glob("*.jsonl"))
    if not audit_files:
        return []
    return [
        json.loads(line)
        for line in audit_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# -- audit fields --------------------------------------------------------


def test_audit_read_entry_records_tokens_in_context_bundle(
    tmp_path: Path, lock_path: Path
) -> None:
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    _seed_vault(vault_root)

    orchestrator = _orchestrator_for(
        config=_load_config("default"),
        config_label="default",
        vault_root=vault_root,
        domains_root=domains_root,
        lock_path=lock_path,
    )

    orchestrator.handle_message("what did I think about consciousness?")

    entries = _read_audit_entries(vault_root)
    read_entries = [e for e in entries if e["op"] == "read"]
    assert len(read_entries) == 1
    record = read_entries[0]
    assert "tokens_in_context_bundle" in record
    assert isinstance(record["tokens_in_context_bundle"], int)
    assert record["tokens_in_context_bundle"] >= 0


def test_audit_read_entry_records_flags(
    tmp_path: Path, lock_path: Path
) -> None:
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    _seed_vault(vault_root)

    orchestrator = _orchestrator_for(
        config=_load_config("default"),
        config_label="default",
        vault_root=vault_root,
        domains_root=domains_root,
        lock_path=lock_path,
    )

    orchestrator.handle_message("what did I think about consciousness?")

    entries = _read_audit_entries(vault_root)
    read_entries = [e for e in entries if e["op"] == "read"]
    record = read_entries[0]
    flags = record.get("flags") or {}
    # Default config = all eight Booleans ON.
    assert flags["tiered_retrieval"] is True
    assert flags["per_domain_shaping"] is True
    assert flags["recency_weighting"] is True
    assert flags["active_session_summary"] is True
    assert flags["vault_index_first"] is True
    assert flags["backlink_expansion"] is True
    assert flags["suggested_actions"] is True
    assert flags["conflict_auto_merge"] is True


def test_audit_read_entry_under_baseline_records_all_off_flags(
    tmp_path: Path, lock_path: Path
) -> None:
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    _seed_vault(vault_root)

    orchestrator = _orchestrator_for(
        config=_load_config("baseline"),
        config_label="baseline",
        vault_root=vault_root,
        domains_root=domains_root,
        lock_path=lock_path,
    )

    orchestrator.handle_message("what did I think about consciousness?")

    entries = _read_audit_entries(vault_root)
    read_entries = [e for e in entries if e["op"] == "read"]
    record = read_entries[0]
    flags = record.get("flags") or {}
    assert flags["tiered_retrieval"] is False
    assert flags["per_domain_shaping"] is False
    assert flags["vault_index_first"] is False
    assert flags["conflict_auto_merge"] is False


def test_audit_read_default_records_higher_token_count_than_baseline(
    tmp_path: Path, lock_path: Path
) -> None:
    """Default config preloads INDEX + session, so its bundle is heavier."""
    # default vault
    default_vault = tmp_path / "vault-default"
    default_domains = tmp_path / "domains-default"
    _seed_journal_domain(default_domains)
    _seed_vault(default_vault)

    default_orch = _orchestrator_for(
        config=_load_config("default"),
        config_label="default",
        vault_root=default_vault,
        domains_root=default_domains,
        lock_path=tmp_path / "default.lock",
    )
    default_orch.handle_message("consciousness?")

    # baseline vault
    baseline_vault = tmp_path / "vault-baseline"
    baseline_domains = tmp_path / "domains-baseline"
    _seed_journal_domain(baseline_domains)
    _seed_vault(baseline_vault)

    baseline_orch = _orchestrator_for(
        config=_load_config("baseline"),
        config_label="baseline",
        vault_root=baseline_vault,
        domains_root=baseline_domains,
        lock_path=tmp_path / "baseline.lock",
    )
    baseline_orch.handle_message("consciousness?")

    default_entries = _read_audit_entries(default_vault)
    baseline_entries = _read_audit_entries(baseline_vault)
    default_tokens = next(
        e for e in default_entries if e["op"] == "read"
    )["tokens_in_context_bundle"]
    baseline_tokens = next(
        e for e in baseline_entries if e["op"] == "read"
    )["tokens_in_context_bundle"]

    assert default_tokens > baseline_tokens
