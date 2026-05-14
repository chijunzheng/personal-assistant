"""Integration: the orchestrator renders reminder/view.py after a write.

Issue #38 retrofits the reminder plugin onto the kernel view primitive
(ADR 2026-05-14). The handler no longer self-calls ``_regenerate_view``;
the kernel's ``_render_domain_view`` post-write hook drives it from
``_handle_reminder_write``, exactly as it already does for finance.

These tests run against the *real* ``domains/`` tree — the reminder
``domain.yaml`` declares the ``view:`` block, so a reminder write
produces a real ``vault/reminder/reminders.md`` and an ``op=view_render``
audit entry with ``domain=reminder``.
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


def _stub_invoker(text: str = "ok"):
    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(text=text, tokens_in=1, tokens_out=1, raw={})

    return _invoke


def _fixed_clock() -> datetime:
    return datetime(2026, 5, 11, 15, 57, 0, tzinfo=timezone.utc)


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
    classifier = _build_classifier(_REAL_DOMAINS_ROOT, "reminder.add")
    extractor = lambda _m, _i: {  # noqa: E731
        "message": "Order IKEA Hemnes for Tracy",
        "fire_at": "2026-05-17T18:00:00+00:00",
        "recurrence": None,
    }
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=audit_root,
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        domains_root=_REAL_DOMAINS_ROOT,
        clock=_fixed_clock,
        chat_id="test-chat",
        reminder_extractor=extractor,
    )
    return orchestrator, vault_root, audit_root


# ---------------------------------------------------------------------------
# Cycle 1 — the kernel hook renders reminders.md after a reminder write
# ---------------------------------------------------------------------------


def test_reminder_write_triggers_kernel_view_render(
    tmp_path: Path, lock_path: Path
) -> None:
    """A reminder.add write triggers the kernel-called view renderer.

    Identical user-visible result to issue #32 — reminders.md still
    regenerates on every write — but the trigger is now the kernel
    post-write hook, not a handler self-call.
    """
    orchestrator, vault_root, _ = _make_orchestrator(tmp_path, lock_path)

    orchestrator.handle_message("remind me Sunday 6pm to order the IKEA Hemnes")

    view_path = vault_root / "reminder" / "reminders.md"
    assert view_path.exists()
    body = view_path.read_text(encoding="utf-8")
    assert "# Reminders" in body
    assert "## Pending" in body
    assert "- [ ] Order IKEA Hemnes for Tracy" in body
    # The generated view carries the machine-checkable marker.
    assert "generated_by:" in body
    assert "domains/reminder/view.py" in body


def test_reminder_view_render_is_audit_logged(
    tmp_path: Path, lock_path: Path
) -> None:
    """A successful reminder view render produces an ``op=view_render`` entry."""
    orchestrator, _, audit_root = _make_orchestrator(tmp_path, lock_path)

    orchestrator.handle_message("remind me Sunday 6pm to order the IKEA Hemnes")

    view_entries = [
        e for e in _audit_entries(audit_root) if e["op"] == "view_render"
    ]
    assert len(view_entries) == 1
    assert view_entries[0]["domain"] == "reminder"
    assert view_entries[0]["outcome"] == "ok"


# ---------------------------------------------------------------------------
# Cycle 2 — a render failure is best-effort: never rolls back the write
# ---------------------------------------------------------------------------


def test_reminder_view_render_failure_does_not_roll_back_write(
    tmp_path: Path, lock_path: Path, monkeypatch
) -> None:
    """A renderer that raises is caught; the canonical events append survives."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        orchestrator_module, "resolve_view_renderer", lambda *a, **k: _boom
    )
    orchestrator, vault_root, audit_root = _make_orchestrator(tmp_path, lock_path)

    reply = orchestrator.handle_message("remind me Sunday 6pm to call mom")

    # The canonical JSONL append is untouched by the render failure.
    events_file = vault_root / "reminder" / "events.jsonl"
    assert events_file.exists()
    rows = [
        line
        for line in events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    # The user still gets a success reply.
    assert "scheduled" in reply.text.lower() or "reminder" in reply.text.lower()
    # The failure is audit-logged as outcome=error on the view_render op.
    view_entries = [
        e for e in _audit_entries(audit_root) if e["op"] == "view_render"
    ]
    assert len(view_entries) == 1
    assert view_entries[0]["outcome"] == "error"
    assert "error" in view_entries[0]


# ---------------------------------------------------------------------------
# Cycle 3 — an idempotent no-op write still renders (the view tracks state)
# ---------------------------------------------------------------------------


def test_reminder_view_renders_even_on_idempotent_write(
    tmp_path: Path, lock_path: Path
) -> None:
    """Re-issuing the same reminder still regenerates reminders.md.

    The handler de-dupes the JSONL append, but the kernel view hook runs
    on every successful write path so the projection never drifts.
    """
    orchestrator, vault_root, audit_root = _make_orchestrator(tmp_path, lock_path)

    orchestrator.handle_message("remind me Sunday 6pm to order the IKEA Hemnes")
    view_path = vault_root / "reminder" / "reminders.md"
    assert view_path.exists()
    view_path.unlink()  # delete the projection

    # Same message again — JSONL append is a no-op, but the view re-renders.
    orchestrator.handle_message("remind me Sunday 6pm to order the IKEA Hemnes")
    assert view_path.exists()

    view_entries = [
        e for e in _audit_entries(audit_root) if e["op"] == "view_render"
    ]
    assert len(view_entries) == 2
    assert all(e["outcome"] == "ok" for e in view_entries)
