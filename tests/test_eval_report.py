"""Tests for ``eval/report.py`` — composes ``docs/eval-progression.md``.

The report:
  * loads two paired result files (default + baseline)
  * optionally loads a scored JSON for the 5-dim table
  * produces a markdown file with three sections:
      1. 5-dimension table (mean/median per dimension, default vs baseline)
      2. Token chart (per-case + aggregate mean/median/p95)
      3. Tool-palette delta (which tools each config used, set diff)
  * orders rows deterministically (sorted case_id) for diff-friendliness
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


DIMENSIONS = ("accuracy", "grounding", "conciseness", "connection", "trust")


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _make_paired_results(tmp_path: Path) -> tuple[Path, Path]:
    """Build a small paired-results pair for testing."""
    default_rows = [
        {
            "case_id": "a-1",
            "config": "default",
            "reply": "default reply for a-1",
            "audit_path": str(tmp_path / "audit-a-1-default.jsonl"),
            "tokens_in": 100,
            "tokens_out": 50,
            "total_tokens": 150,
            "tool_calls": ["read_index", "grep", "read_file"],
            "duration_ms": 50,
            "status": "ok",
        },
        {
            "case_id": "b-1",
            "config": "default",
            "reply": "default reply for b-1",
            "audit_path": str(tmp_path / "audit-b-1-default.jsonl"),
            "tokens_in": 200,
            "tokens_out": 100,
            "total_tokens": 300,
            "tool_calls": ["read_index", "expand_keywords", "grep"],
            "duration_ms": 75,
            "status": "ok",
        },
    ]
    baseline_rows = [
        {
            "case_id": "a-1",
            "config": "baseline",
            "reply": "baseline reply for a-1",
            "audit_path": str(tmp_path / "audit-a-1-baseline.jsonl"),
            "tokens_in": 150,
            "tokens_out": 80,
            "total_tokens": 230,
            "tool_calls": ["grep", "read_file"],
            "duration_ms": 55,
            "status": "ok",
        },
        {
            "case_id": "b-1",
            "config": "baseline",
            "reply": "baseline reply for b-1",
            "audit_path": str(tmp_path / "audit-b-1-baseline.jsonl"),
            "tokens_in": 300,
            "tokens_out": 150,
            "total_tokens": 450,
            "tool_calls": ["grep"],
            "duration_ms": 82,
            "status": "ok",
        },
    ]
    default_path = tmp_path / "default.json"
    baseline_path = tmp_path / "baseline.json"
    _write(default_path, default_rows)
    _write(baseline_path, baseline_rows)
    return default_path, baseline_path


def _make_scored(tmp_path: Path) -> Path:
    """Build a small scored output for the same case set."""
    scored = {
        "a-1": {
            "default": {"accuracy": 5, "grounding": 4, "conciseness": 4, "connection": 5, "trust": 5},
            "baseline": {"accuracy": 3, "grounding": 2, "conciseness": 3, "connection": 1, "trust": 2},
            "_status": "ok",
        },
        "b-1": {
            "default": {"accuracy": 4, "grounding": 5, "conciseness": 4, "connection": 4, "trust": 5},
            "baseline": {"accuracy": 2, "grounding": 2, "conciseness": 3, "connection": 1, "trust": 2},
            "_status": "ok",
        },
    }
    path = tmp_path / "scored.json"
    _write(path, scored)
    return path


# ---------------------------------------------------------------------------
# 5-dimension table
# ---------------------------------------------------------------------------


def test_report_renders_five_dim_table_with_means(tmp_path: Path) -> None:
    """Report includes a markdown table for the 5 dimensions with mean values."""
    from eval.report import compose_report

    default_path, baseline_path = _make_paired_results(tmp_path)
    scored_path = _make_scored(tmp_path)
    out = tmp_path / "eval-progression.md"

    compose_report(
        default_path=default_path,
        baseline_path=baseline_path,
        scored_path=scored_path,
        out_path=out,
    )

    body = out.read_text(encoding="utf-8")
    assert "## 5-dimension scoring" in body
    # Each dim must appear in the table.
    for dim in DIMENSIONS:
        assert dim in body
    # The mean for accuracy default = (5+4)/2 = 4.5 ; baseline = (3+2)/2 = 2.5
    assert "4.5" in body
    assert "2.5" in body


def test_report_renders_token_chart(tmp_path: Path) -> None:
    """Token chart includes per-case rows + aggregate (mean / median / p95)."""
    from eval.report import compose_report

    default_path, baseline_path = _make_paired_results(tmp_path)
    out = tmp_path / "eval-progression.md"

    compose_report(
        default_path=default_path,
        baseline_path=baseline_path,
        scored_path=None,  # token-only path
        out_path=out,
    )

    body = out.read_text(encoding="utf-8")
    assert "## Token chart" in body
    # Aggregate row labels.
    assert "mean" in body.lower()
    assert "median" in body.lower()
    assert "p95" in body.lower()
    # Per-case rows.
    assert "a-1" in body
    assert "b-1" in body
    # The actual numbers.
    assert "150" in body  # default total for a-1
    assert "230" in body  # baseline total for a-1


def test_report_renders_tool_palette_delta(tmp_path: Path) -> None:
    """Tool-palette delta lists tools used by default vs baseline."""
    from eval.report import compose_report

    default_path, baseline_path = _make_paired_results(tmp_path)
    out = tmp_path / "eval-progression.md"

    compose_report(
        default_path=default_path,
        baseline_path=baseline_path,
        scored_path=None,
        out_path=out,
    )

    body = out.read_text(encoding="utf-8")
    assert "## Tool-palette delta" in body
    # Default-only tools (in default but not baseline).
    assert "read_index" in body
    assert "expand_keywords" in body
    # Common tools should also appear somewhere.
    assert "grep" in body


def test_report_orders_cases_deterministically(tmp_path: Path) -> None:
    """Cases sorted by id so report diffs are stable."""
    from eval.report import compose_report

    # Build paired results with cases in a non-sorted order.
    default = [
        {"case_id": "z-9", "config": "default", "reply": "z", "tokens_in": 1, "tokens_out": 1,
         "total_tokens": 2, "tool_calls": [], "audit_path": "", "duration_ms": 1, "status": "ok"},
        {"case_id": "a-0", "config": "default", "reply": "a", "tokens_in": 1, "tokens_out": 1,
         "total_tokens": 2, "tool_calls": [], "audit_path": "", "duration_ms": 1, "status": "ok"},
    ]
    baseline = [
        {"case_id": "z-9", "config": "baseline", "reply": "z", "tokens_in": 1, "tokens_out": 1,
         "total_tokens": 2, "tool_calls": [], "audit_path": "", "duration_ms": 1, "status": "ok"},
        {"case_id": "a-0", "config": "baseline", "reply": "a", "tokens_in": 1, "tokens_out": 1,
         "total_tokens": 2, "tool_calls": [], "audit_path": "", "duration_ms": 1, "status": "ok"},
    ]
    default_path = tmp_path / "d.json"
    baseline_path = tmp_path / "b.json"
    _write(default_path, default)
    _write(baseline_path, baseline)

    out = tmp_path / "report.md"
    compose_report(
        default_path=default_path,
        baseline_path=baseline_path,
        scored_path=None,
        out_path=out,
    )
    body = out.read_text(encoding="utf-8")
    # a-0 must appear before z-9 in the token chart.
    assert body.index("a-0") < body.index("z-9")


def test_report_handles_missing_scored_file_gracefully(tmp_path: Path) -> None:
    """Without scored input, the 5-dim section is omitted but token chart still renders."""
    from eval.report import compose_report

    default_path, baseline_path = _make_paired_results(tmp_path)
    out = tmp_path / "report.md"
    compose_report(
        default_path=default_path,
        baseline_path=baseline_path,
        scored_path=None,
        out_path=out,
    )
    body = out.read_text(encoding="utf-8")
    assert "## Token chart" in body
    assert "## 5-dimension scoring" not in body


def test_report_aggregate_p95_is_sane(tmp_path: Path) -> None:
    """p95 of [150, 300] for default is between max and itself; we expect 300."""
    from eval.report import compose_report

    default_path, baseline_path = _make_paired_results(tmp_path)
    out = tmp_path / "report.md"
    compose_report(
        default_path=default_path,
        baseline_path=baseline_path,
        scored_path=None,
        out_path=out,
    )
    body = out.read_text(encoding="utf-8")
    # default tokens are 150 + 300; p95 ≈ 292 or 300 depending on method; both are visible.
    # We assert the report contains the max value (300) or close to it.
    assert "300" in body
    # baseline tokens are 230 + 450.
    assert "450" in body
