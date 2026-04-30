"""Tests for the eval head-to-head harness (``eval/run.py``).

The harness:
  * walks ``domains/*/eval/cases.jsonl`` AND ``eval/cases/*.jsonl``,
    de-duplicating by ``case.id``
  * for each case, materializes ``vault_setup`` (if any) into a TEMP vault
  * runs the case under both configs (``default``, ``baseline``)
  * captures reply text, audit lines, token totals, tool-call sequence
  * writes a deterministic-schema JSON results file under ``eval/results/``

These tests pin the **observable** behavior (filesystem effects + result
schema). The runner does not actually call the LLM — we inject a stub
``invoke_case`` so token counts and tool-call sequences are deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def _write_cases(path: Path, cases: list[dict[str, Any]]) -> None:
    """Helper: write a JSONL file with one case per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case) + "\n")


# ---------------------------------------------------------------------------
# Case discovery
# ---------------------------------------------------------------------------


def test_discover_cases_finds_per_domain_jsonl(tmp_path: Path) -> None:
    """Cases under ``domains/<name>/eval/cases.jsonl`` are auto-discovered."""
    from eval.run import discover_cases

    fitness_cases = tmp_path / "domains" / "fitness" / "eval" / "cases.jsonl"
    finance_cases = tmp_path / "domains" / "finance" / "eval" / "cases.jsonl"
    _write_cases(fitness_cases, [{"id": "fit-001", "input": "hi"}])
    _write_cases(finance_cases, [{"id": "fin-001", "input": "hi"}])

    found = discover_cases(project_root=tmp_path)

    ids = sorted(c["id"] for c in found)
    assert ids == ["fin-001", "fit-001"]


def test_discover_cases_finds_top_level_synthetic_cases(tmp_path: Path) -> None:
    """``eval/cases/*.jsonl`` is also walked."""
    from eval.run import discover_cases

    synth = tmp_path / "eval" / "cases" / "synthetic.jsonl"
    _write_cases(synth, [{"id": "syn-001", "input": "hi"}])

    found = discover_cases(project_root=tmp_path)

    assert [c["id"] for c in found] == ["syn-001"]


def test_discover_cases_dedupes_by_id(tmp_path: Path) -> None:
    """If the same case id appears in both locations, only one copy is returned."""
    from eval.run import discover_cases

    domain = tmp_path / "domains" / "fitness" / "eval" / "cases.jsonl"
    synth = tmp_path / "eval" / "cases" / "synthetic.jsonl"
    _write_cases(domain, [{"id": "shared-001", "input": "from-domain"}])
    _write_cases(synth, [{"id": "shared-001", "input": "from-synth"}])

    found = discover_cases(project_root=tmp_path)

    assert len(found) == 1
    assert found[0]["id"] == "shared-001"


def test_discover_cases_attaches_source_path(tmp_path: Path) -> None:
    """Each discovered case carries the relative source path (debuggability)."""
    from eval.run import discover_cases

    fitness_cases = tmp_path / "domains" / "fitness" / "eval" / "cases.jsonl"
    _write_cases(fitness_cases, [{"id": "fit-001", "input": "hi"}])

    found = discover_cases(project_root=tmp_path)

    assert "_source" in found[0]
    assert "domains/fitness/eval/cases.jsonl" in found[0]["_source"]


# ---------------------------------------------------------------------------
# vault_setup materialization
# ---------------------------------------------------------------------------


def test_materialize_vault_setup_yaml_dict(tmp_path: Path) -> None:
    """A ``profile.yaml`` dict gets serialized as YAML to ``vault/fitness/profile.yaml``."""
    from eval.run import materialize_vault_setup

    vault_root = tmp_path / "vault"
    spec = {"profile.yaml": {"goal": "recomp", "weekly_training_days": 4}}

    materialize_vault_setup(spec, vault_root=vault_root, domain="fitness")

    assert (vault_root / "fitness" / "profile.yaml").exists()
    body = (vault_root / "fitness" / "profile.yaml").read_text(encoding="utf-8")
    assert "goal: recomp" in body


def test_materialize_vault_setup_jsonl_recent(tmp_path: Path) -> None:
    """``workouts.jsonl_recent`` dates of ``T-1`` etc. resolve to real ISO dates."""
    from eval.run import materialize_vault_setup
    from datetime import date

    vault_root = tmp_path / "vault"
    today = date(2026, 4, 29)
    spec = {
        "workouts.jsonl_recent": [
            {"date": "T-1", "type": "strength"},
            {"date": "T-3", "type": "cardio"},
        ]
    }

    materialize_vault_setup(spec, vault_root=vault_root, domain="fitness", today=today)

    target = vault_root / "fitness" / "workouts.jsonl"
    assert target.exists()
    rows = [json.loads(l) for l in target.read_text().splitlines() if l.strip()]
    assert rows[0]["date"] == "2026-04-28"  # T-1
    assert rows[1]["date"] == "2026-04-26"  # T-3
    assert rows[0]["type"] == "strength"


def test_materialize_vault_setup_markdown_relative_date(tmp_path: Path) -> None:
    """``journal/T-1.md`` becomes ``journal/<resolved-date>.md`` with the body."""
    from eval.run import materialize_vault_setup
    from datetime import date

    vault_root = tmp_path / "vault"
    today = date(2026, 4, 29)
    spec = {"journal/T-1.md": "exhausted today. Bad sleep."}

    materialize_vault_setup(spec, vault_root=vault_root, domain="fitness", today=today)

    target = vault_root / "journal" / "2026-04-28.md"
    assert target.exists()
    assert "exhausted today" in target.read_text(encoding="utf-8")


def test_materialize_vault_setup_inventory_state(tmp_path: Path) -> None:
    """``inventory/state.yaml`` is written under the inventory directory verbatim."""
    from eval.run import materialize_vault_setup

    vault_root = tmp_path / "vault"
    spec = {
        "inventory/state.yaml": {
            "items": [{"item": "milk", "quantity": 1, "unit": "L"}]
        }
    }

    materialize_vault_setup(spec, vault_root=vault_root, domain="fitness")

    target = vault_root / "inventory" / "state.yaml"
    assert target.exists()
    assert "milk" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-case execution under both configs
# ---------------------------------------------------------------------------


def _stub_invoke_case_factory(reply: str, tokens_in: int, tokens_out: int, tool_calls: list[str]):
    """Return a stub matching the ``invoke_case`` contract used by the runner."""

    def _invoke(case, *, config_label, vault_root, project_root):
        return {
            "reply": reply,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tool_calls": list(tool_calls),
            "duration_ms": 12,
            "audit_lines": [
                {
                    "op": "classify",
                    "intent": case.get("intent", "unknown"),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                }
            ],
            "status": "ok",
        }

    return _invoke


def test_run_case_executes_under_both_configs(tmp_path: Path) -> None:
    """A single case is run under default AND baseline; both rows land in results."""
    from eval.run import run_cases

    case = {"id": "smoke-1", "input": "hi", "intent": "journal.query"}
    invoker = _stub_invoke_case_factory("ok", tokens_in=10, tokens_out=20, tool_calls=["read_index"])

    results = run_cases(
        cases=[case],
        results_dir=tmp_path / "results",
        invoke_case=invoker,
        project_root=tmp_path,
        timestamp="20260429-120000",
    )

    # Two result files: default + baseline.
    assert (tmp_path / "results" / "default-20260429-120000.json").exists()
    assert (tmp_path / "results" / "baseline-20260429-120000.json").exists()

    default_rows = json.loads((tmp_path / "results" / "default-20260429-120000.json").read_text())
    baseline_rows = json.loads((tmp_path / "results" / "baseline-20260429-120000.json").read_text())

    assert len(default_rows) == 1
    assert len(baseline_rows) == 1
    assert default_rows[0]["case_id"] == "smoke-1"
    assert default_rows[0]["config"] == "default"
    assert baseline_rows[0]["config"] == "baseline"


def test_run_case_captures_token_telemetry_and_tool_calls(tmp_path: Path) -> None:
    """Each result row carries ``tokens_in``, ``tokens_out``, ``total_tokens``, ``tool_calls``."""
    from eval.run import run_cases

    case = {"id": "smoke-2", "input": "x", "intent": "fitness.query"}
    invoker = _stub_invoke_case_factory(
        "reply text",
        tokens_in=100,
        tokens_out=50,
        tool_calls=["read_index", "grep", "read_file"],
    )

    run_cases(
        cases=[case],
        results_dir=tmp_path / "results",
        invoke_case=invoker,
        project_root=tmp_path,
        timestamp="20260429-130000",
    )

    rows = json.loads((tmp_path / "results" / "default-20260429-130000.json").read_text())
    row = rows[0]
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 50
    assert row["total_tokens"] == 150
    assert row["tool_calls"] == ["read_index", "grep", "read_file"]
    assert row["status"] == "ok"
    assert row["reply"] == "reply text"
    assert "duration_ms" in row


def test_run_case_marks_status_error_on_invoker_exception(tmp_path: Path) -> None:
    """An exception raised by ``invoke_case`` is captured as ``status=error``."""
    from eval.run import run_cases

    def _bad_invoker(case, *, config_label, vault_root, project_root):
        raise RuntimeError("simulated LLM crash")

    case = {"id": "bad-1", "input": "x", "intent": "journal.query"}

    run_cases(
        cases=[case],
        results_dir=tmp_path / "results",
        invoke_case=_bad_invoker,
        project_root=tmp_path,
        timestamp="20260429-140000",
    )

    rows = json.loads((tmp_path / "results" / "default-20260429-140000.json").read_text())
    assert rows[0]["status"] == "error"
    assert "simulated LLM crash" in rows[0]["error"]
    assert rows[0]["total_tokens"] == 0


def test_run_case_materializes_vault_setup_per_case(tmp_path: Path) -> None:
    """If the case has ``vault_setup``, the invoker sees the materialized vault path."""
    from eval.run import run_cases

    seen_vault_files: list[Path] = []

    def _capturing_invoker(case, *, config_label, vault_root, project_root):
        # Capture which files exist in the vault when this case is run.
        for p in Path(vault_root).rglob("*"):
            if p.is_file():
                seen_vault_files.append(p.relative_to(vault_root))
        return {
            "reply": "ok",
            "tokens_in": 1,
            "tokens_out": 1,
            "tool_calls": [],
            "duration_ms": 1,
            "audit_lines": [],
            "status": "ok",
        }

    case = {
        "id": "vault-setup-1",
        "input": "what should I train today?",
        "intent": "fitness.workout_plan",
        "vault_setup": {
            "profile.yaml": {"goal": "recomp"},
        },
    }

    run_cases(
        cases=[case],
        results_dir=tmp_path / "results",
        invoke_case=_capturing_invoker,
        project_root=tmp_path,
        timestamp="20260429-150000",
    )

    # The fixture file must have existed in the per-case temp vault when the
    # invoker ran. Since fitness vault_setup dumps to vault/fitness/<file>.
    assert any(str(p).endswith("profile.yaml") for p in seen_vault_files)


def test_run_case_audit_path_is_recorded(tmp_path: Path) -> None:
    """Each result row references the audit file the case produced."""
    from eval.run import run_cases

    invoker = _stub_invoke_case_factory("ok", tokens_in=1, tokens_out=1, tool_calls=[])

    run_cases(
        cases=[{"id": "audit-1", "input": "x", "intent": "journal.query"}],
        results_dir=tmp_path / "results",
        invoke_case=invoker,
        project_root=tmp_path,
        timestamp="20260429-160000",
    )

    rows = json.loads((tmp_path / "results" / "default-20260429-160000.json").read_text())
    audit_path = rows[0]["audit_path"]
    assert Path(audit_path).exists()
    # Audit was written by the harness — at least one JSONL line.
    raw = Path(audit_path).read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(l) for l in raw if l.strip()]
    assert any(p["op"] == "classify" for p in parsed)


def test_run_supports_limit_argument(tmp_path: Path) -> None:
    """``--limit N`` only runs the first N cases."""
    from eval.run import run_cases

    cases = [
        {"id": f"c-{i}", "input": "x", "intent": "journal.query"} for i in range(5)
    ]
    invoker = _stub_invoke_case_factory("ok", tokens_in=1, tokens_out=1, tool_calls=[])

    run_cases(
        cases=cases,
        results_dir=tmp_path / "results",
        invoke_case=invoker,
        project_root=tmp_path,
        timestamp="20260429-170000",
        limit=2,
    )

    rows = json.loads((tmp_path / "results" / "default-20260429-170000.json").read_text())
    assert len(rows) == 2
    assert [r["case_id"] for r in rows] == ["c-0", "c-1"]
