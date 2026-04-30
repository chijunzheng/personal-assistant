"""Integration: orchestrator wires classifier -> finance handler -> audit -> reply.

Verifies the new ``finance.*`` dispatch in ``Orchestrator.handle_message``:

  - ``finance.transaction`` invokes the finance write path; the JSONL log
    appears under ``vault/finance/transactions.jsonl`` and an audit entry
    with ``op=write`` + ``domain=finance`` is appended.
  - Re-sending the same statement does not produce duplicate rows.
  - ``finance.query`` invokes the read path; the audit log gets a
    ``op=read`` entry and the reply text contains a real number.

Both ``claude_runner`` and the LLM-backed extractor are stubbed so unit
tests don't shell out.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from kernel.classifier import Classifier
from kernel.claude_runner import ClaudeResponse
from kernel.orchestrator import Orchestrator, SingleInstanceLock


_SAMPLE_TXNS = [
    {
        "date": "2026-03-04",
        "amount": -4.75,
        "currency": "CAD",
        "merchant": "STARBUCKS #1234",
        "merchant_normalized": "Starbucks",
        "category": "food",
        "subcategory": "coffee",
        "raw": "2026-03-04 STARBUCKS #1234 CAD -4.75",
        "tags": [],
        "confidence": 0.95,
    },
    {
        "date": "2026-03-12",
        "amount": -3.50,
        "currency": "CAD",
        "merchant": "TIM HORTONS",
        "merchant_normalized": "Tim Hortons",
        "category": "food",
        "subcategory": "coffee",
        "raw": "2026-03-12 TIM HORTONS CAD -3.50",
        "tags": [],
        "confidence": 0.92,
    },
]


def _stub_invoker(text: str = "ok", tokens_in: int = 1, tokens_out: int = 1):
    """A claude_runner.invoke-shaped stub that records what it last saw."""
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


def _seed_finance_domain(domains_root: Path) -> None:
    """Drop a finance ``domain.yaml`` so the classifier knows the intent label."""
    domain_dir = domains_root / "finance"
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "domain.yaml").write_text(
        "name: finance\n"
        "description: \"transactions + spending queries\"\n"
        "intents:\n"
        "  - finance.transaction\n"
        "  - finance.query\n",
        encoding="utf-8",
    )


def _build_classifier(domains_root: Path, intent: str) -> Classifier:
    """A Classifier whose LLM call always returns the given intent label."""
    return Classifier(
        domains_root=domains_root,
        invoker=_stub_invoker(text=intent),
        prompt_template="",
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def _seed_transactions(vault_root: Path) -> Path:
    """Write the sample transactions JSONL directly so query tests start populated."""
    from domains.finance.handler import transaction_id

    finance = vault_root / "finance"
    finance.mkdir(parents=True, exist_ok=True)
    txn_file = finance / "transactions.jsonl"
    with open(txn_file, "w", encoding="utf-8") as fh:
        for row in _SAMPLE_TXNS:
            stamped = dict(row)
            stamped["id"] = transaction_id(
                date=row["date"],
                amount=row["amount"],
                merchant=row["merchant"],
                raw=row["raw"],
            )
            stamped["source"] = "test"
            fh.write(json.dumps(stamped) + "\n")
    return txn_file


def _audit_entries(audit_root: Path) -> list[dict]:
    """Return every audit entry across all daily files (sorted by ts)."""
    entries: list[dict] = []
    for daily in sorted(audit_root.glob("*.jsonl")):
        for line in daily.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------------------
# finance.transaction -> write path
# ---------------------------------------------------------------------------


def test_finance_transaction_extracts_and_appends_jsonl(
    tmp_path: Path, lock_path: Path
) -> None:
    """A finance.transaction intent persists rows to the canonical JSONL."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_finance_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "finance.transaction")

    # The orchestrator passes an extractor through to the handler so the
    # LLM doesn't actually run during the test.
    extractor = lambda _text: list(_SAMPLE_TXNS)  # noqa: E731

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        finance_extractor=extractor,
    )

    reply = orchestrator.handle_message("(statement text — extracted upstream)")

    txn_file = vault_root / "finance" / "transactions.jsonl"
    assert txn_file.exists()
    rows = [
        json.loads(line)
        for line in txn_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == len(_SAMPLE_TXNS)
    # Reply confirms the operation.
    assert "transaction" in reply.text.lower() or str(len(rows)) in reply.text


def test_finance_transaction_is_idempotent_in_orchestrator(
    tmp_path: Path, lock_path: Path
) -> None:
    """Re-sending the same statement does not duplicate rows."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_finance_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "finance.transaction")
    extractor = lambda _text: list(_SAMPLE_TXNS)  # noqa: E731

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        finance_extractor=extractor,
    )

    orchestrator.handle_message("statement")
    orchestrator.handle_message("statement")

    txn_file = vault_root / "finance" / "transactions.jsonl"
    rows = [
        json.loads(line)
        for line in txn_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == len(_SAMPLE_TXNS)


def test_finance_transaction_audit_records_write(
    tmp_path: Path, lock_path: Path
) -> None:
    """The orchestrator writes one audit entry with op=write + domain=finance."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_finance_domain(domains_root)
    audit_root = vault_root / "_audit"

    classifier = _build_classifier(domains_root, "finance.transaction")
    extractor = lambda _text: list(_SAMPLE_TXNS)  # noqa: E731

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        finance_extractor=extractor,
    )

    orchestrator.handle_message("statement")

    write_entries = [e for e in _audit_entries(audit_root) if e["op"] == "write"]
    assert len(write_entries) == 1
    assert write_entries[0]["domain"] == "finance"
    assert write_entries[0]["intent"] == "finance.transaction"
    assert write_entries[0]["outcome"] == "ok"
    assert "transactions.jsonl" in write_entries[0]["path"]


# ---------------------------------------------------------------------------
# finance.query -> read path
# ---------------------------------------------------------------------------


def test_finance_query_returns_real_number(tmp_path: Path, lock_path: Path) -> None:
    """A finance.query reply contains the structured numeric answer."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_finance_domain(domains_root)
    _seed_transactions(vault_root)

    classifier = _build_classifier(domains_root, "finance.query")
    parser = lambda _q: {  # noqa: E731
        "category": "coffee",
        "date_range": ("2026-03-01", "2026-03-31"),
        "agg": "sum",
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        finance_query_parser=parser,
    )

    reply = orchestrator.handle_message("how much did I spend on coffee last month?")

    # -4.75 + -3.50 = -8.25 — the reply must contain that figure.
    assert "8.25" in reply.text


def test_finance_query_audit_records_read(tmp_path: Path, lock_path: Path) -> None:
    """A finance.query produces a read audit entry with domain=finance."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_finance_domain(domains_root)
    _seed_transactions(vault_root)

    classifier = _build_classifier(domains_root, "finance.query")
    parser = lambda _q: {  # noqa: E731
        "category": "coffee",
        "date_range": ("2026-03-01", "2026-03-31"),
        "agg": "sum",
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        finance_query_parser=parser,
    )

    orchestrator.handle_message("coffee in march?")

    read_entries = [
        e for e in _audit_entries(vault_root / "_audit") if e["op"] == "read"
    ]
    assert len(read_entries) == 1
    assert read_entries[0]["domain"] == "finance"
    assert read_entries[0]["intent"] == "finance.query"
    assert read_entries[0]["outcome"] == "ok"


def test_finance_unrelated_intents_dont_route_to_finance(
    tmp_path: Path, lock_path: Path
) -> None:
    """Journal intents must not write to ``vault/finance/`` — plugin isolation."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"

    # Seed both domains.
    _seed_finance_domain(domains_root)
    journal_dir = domains_root / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "domain.yaml").write_text(
        "name: journal\n"
        "intents:\n"
        "  - journal.capture\n",
        encoding="utf-8",
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
    # No finance JSONL was created.
    assert not (vault_root / "finance" / "transactions.jsonl").exists()
