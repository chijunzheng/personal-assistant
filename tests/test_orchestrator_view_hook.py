"""Integration: the orchestrator renders a domain's view after a write.

ADR 2026-05-14, change 2: a kernel ``_render_domain_view`` helper runs in
each ``_handle_*_write`` path, right next to ``_maybe_refresh_index``. It
resolves the domain's ``view:`` declaration data-drivenly, lazily imports
the renderer, and calls it. View regeneration is best-effort: wrapped in
try/except, audit-logged as ``op=view_render``, and a render failure can
never roll back the canonical write that already landed.

These tests run against the *real* ``domains/`` tree — plugins are imported
as ``domains.<name>``, exactly as the orchestrator does in production. The
finance domain.yaml declares the ``view:`` block, so a finance.transaction
write renders real ``vault/finance/YYYY-MM.md`` rollups.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import kernel.orchestrator as orchestrator_module
from kernel.classifier import Classifier
from kernel.claude_runner import ClaudeResponse
from kernel.orchestrator import Orchestrator, SingleInstanceLock

# The real domains/ tree — the orchestrator imports plugins from here.
_REAL_DOMAINS_ROOT = Path(__file__).resolve().parent.parent / "domains"


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
]


def _stub_invoker(text: str = "ok"):
    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(text=text, tokens_in=1, tokens_out=1, raw={})

    return _invoke


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def _build_classifier(domains_root: Path, intent: str) -> Classifier:
    return Classifier(
        domains_root=domains_root,
        invoker=_stub_invoker(text=intent),
        prompt_template="",
    )


def _audit_entries(audit_root: Path) -> list[dict]:
    entries: list[dict] = []
    for daily in sorted(audit_root.glob("*.jsonl")):
        for line in daily.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


def _make_orchestrator(tmp_path: Path, lock_path: Path):
    """Build an orchestrator wired to the real domains tree + a temp vault."""
    vault_root = tmp_path / "vault"
    audit_root = vault_root / "_audit"
    classifier = _build_classifier(_REAL_DOMAINS_ROOT, "finance.transaction")
    extractor = lambda _t: list(_SAMPLE_TXNS)  # noqa: E731
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        domains_root=_REAL_DOMAINS_ROOT,
        clock=_fixed_clock,
        chat_id="test-chat",
        finance_extractor=extractor,
    )
    return orchestrator, vault_root, audit_root


# ---------------------------------------------------------------------------
# Cycle 1 — a declared view is rendered after a successful write
# ---------------------------------------------------------------------------


def test_view_renderer_runs_after_finance_write(
    tmp_path: Path, lock_path: Path
) -> None:
    """A finance.transaction write triggers the registered view renderer.

    The sample transaction is dated 2026-03, so the renderer must produce
    ``vault/finance/2026-03.md`` carrying the generated_by: marker.
    """
    orchestrator, vault_root, _ = _make_orchestrator(tmp_path, lock_path)

    orchestrator.handle_message("statement")

    rollup = vault_root / "finance" / "2026-03.md"
    assert rollup.exists()
    text = rollup.read_text(encoding="utf-8")
    assert "generated_by:" in text
    assert "Starbucks" in text


def test_view_render_is_audit_logged(tmp_path: Path, lock_path: Path) -> None:
    """A successful view render produces an ``op=view_render`` audit entry."""
    orchestrator, _, audit_root = _make_orchestrator(tmp_path, lock_path)

    orchestrator.handle_message("statement")

    view_entries = [
        e for e in _audit_entries(audit_root) if e["op"] == "view_render"
    ]
    assert len(view_entries) == 1
    assert view_entries[0]["domain"] == "finance"
    assert view_entries[0]["outcome"] == "ok"


# ---------------------------------------------------------------------------
# Cycle 2 — no view: block declared -> hook is a silent no-op
# ---------------------------------------------------------------------------


def test_no_view_block_is_silent_noop(
    tmp_path: Path, lock_path: Path, monkeypatch
) -> None:
    """A domain whose view resolves to None still writes; no view audit entry.

    Simulated by forcing the resolver to return ``None`` — the same path a
    pure-markdown domain (no ``view:`` block) takes in production.
    """
    monkeypatch.setattr(
        orchestrator_module, "resolve_view_renderer", lambda *a, **k: None
    )
    orchestrator, vault_root, audit_root = _make_orchestrator(tmp_path, lock_path)

    orchestrator.handle_message("statement")

    # Canonical write still landed.
    assert (vault_root / "finance" / "transactions.jsonl").exists()
    # No view_render audit entry, and no rollup file.
    assert not [
        e for e in _audit_entries(audit_root) if e["op"] == "view_render"
    ]
    assert not (vault_root / "finance" / "2026-03.md").exists()


# ---------------------------------------------------------------------------
# Cycle 3 — a render failure is best-effort: never rolls back the write
# ---------------------------------------------------------------------------


def test_view_render_failure_does_not_roll_back_write(
    tmp_path: Path, lock_path: Path, monkeypatch
) -> None:
    """A renderer that raises is caught; the canonical append survives."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        orchestrator_module, "resolve_view_renderer", lambda *a, **k: _boom
    )
    orchestrator, vault_root, audit_root = _make_orchestrator(tmp_path, lock_path)

    reply = orchestrator.handle_message("statement")

    # The canonical JSONL append is untouched by the render failure.
    txn_file = vault_root / "finance" / "transactions.jsonl"
    assert txn_file.exists()
    rows = [
        line
        for line in txn_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == len(_SAMPLE_TXNS)
    # The user still gets a success reply.
    assert "Recorded" in reply.text or "transaction" in reply.text.lower()
    # The failure is audit-logged as outcome=error on the view_render op.
    view_entries = [
        e for e in _audit_entries(audit_root) if e["op"] == "view_render"
    ]
    assert len(view_entries) == 1
    assert view_entries[0]["outcome"] == "error"
    assert "error" in view_entries[0]
