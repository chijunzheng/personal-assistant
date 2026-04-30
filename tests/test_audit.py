"""Tests for ``kernel.audit`` — append-only JSONL audit log."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kernel.audit import AuditEntry, write_audit_entry


def _valid_entry(**overrides) -> dict:
    """A minimal, fully-populated audit entry payload — overridable."""
    base = {
        "ts": datetime(2026, 4, 29, 22, 47, 1, tzinfo=timezone.utc).isoformat(),
        "op": "echo",
        "actor": "kernel.orchestrator",
        "outcome": "ok",
        "duration_ms": 142,
        "config": "default",
    }
    base.update(overrides)
    return base


def test_write_audit_entry_creates_dated_jsonl_file(tmp_path: Path) -> None:
    """The audit writer rotates daily under ``<root>/YYYY-MM-DD.jsonl``."""
    write_audit_entry(_valid_entry(), audit_root=tmp_path)

    expected = tmp_path / "2026-04-29.jsonl"
    assert expected.exists()
    line = expected.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["op"] == "echo"
    assert record["actor"] == "kernel.orchestrator"
    assert record["config"] == "default"


def test_write_audit_entry_rejects_missing_required_field(tmp_path: Path) -> None:
    """A payload missing a required field is not silently logged."""
    incomplete = _valid_entry()
    del incomplete["actor"]

    with pytest.raises(ValueError, match="actor"):
        write_audit_entry(incomplete, audit_root=tmp_path)

    # And nothing was written despite the failure.
    assert list(tmp_path.iterdir()) == []


def test_write_audit_entry_appends_subsequent_entries(tmp_path: Path) -> None:
    """A second write does not clobber the first — the daily file accumulates."""
    write_audit_entry(_valid_entry(op="first"), audit_root=tmp_path)
    write_audit_entry(_valid_entry(op="second"), audit_root=tmp_path)

    daily = tmp_path / "2026-04-29.jsonl"
    lines = [
        json.loads(raw)
        for raw in daily.read_text(encoding="utf-8").splitlines()
        if raw.strip()
    ]
    assert [entry["op"] for entry in lines] == ["first", "second"]


def test_write_audit_entry_derives_stable_id(tmp_path: Path) -> None:
    """Identical payloads produce identical ``id`` values (sha256-based idempotency)."""
    a = write_audit_entry(_valid_entry(op="same"), audit_root=tmp_path)
    b = write_audit_entry(_valid_entry(op="same"), audit_root=tmp_path / "other")

    assert a.id == b.id
    assert len(a.id) == 64  # sha256 hex digest


def test_write_audit_entry_persists_optional_token_fields(tmp_path: Path) -> None:
    """Token telemetry parsed from claude -p must round-trip into the audit file."""
    write_audit_entry(
        _valid_entry(tokens_in=42, tokens_out=137),
        audit_root=tmp_path,
    )

    record = json.loads(
        (tmp_path / "2026-04-29.jsonl").read_text(encoding="utf-8").strip()
    )
    assert record["tokens_in"] == 42
    assert record["tokens_out"] == 137
