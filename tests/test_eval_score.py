"""Tests for ``eval/score.py`` — manual 5-dim Likert scorer (v1).

The scorer takes two paired result files (one default, one baseline) and
attaches per-case scores on five dimensions:

  * accuracy
  * grounding
  * conciseness
  * connection
  * trust

Each on a 1-5 Likert. v1 is manual (interactive CLI). For test-friendliness,
the scorer also supports ``--non-interactive`` mode that pre-loads a JSON
file of scores.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


DIMENSIONS = ("accuracy", "grounding", "conciseness", "connection", "trust")


def _write_results(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Non-interactive scoring round-trip
# ---------------------------------------------------------------------------


def test_score_non_interactive_writes_scored_json(tmp_path: Path) -> None:
    """Non-interactive scoring with a pre-filled JSON file produces a valid output."""
    from eval.score import score_paired

    default = tmp_path / "default.json"
    baseline = tmp_path / "baseline.json"
    _write_results(default, [{"case_id": "a", "config": "default", "reply": "ok"}])
    _write_results(baseline, [{"case_id": "a", "config": "baseline", "reply": "ok"}])

    prefill = tmp_path / "prefill.json"
    prefill.write_text(json.dumps({
        "a": {
            "default": {"accuracy": 5, "grounding": 4, "conciseness": 4, "connection": 5, "trust": 5},
            "baseline": {"accuracy": 3, "grounding": 2, "conciseness": 3, "connection": 1, "trust": 2},
        }
    }))

    out = tmp_path / "scored.json"
    score_paired(
        default_path=default,
        baseline_path=baseline,
        out_path=out,
        non_interactive_path=prefill,
    )

    scored = json.loads(out.read_text())
    # Output schema: per-case nested by config, with all 5 dims present.
    assert scored["a"]["default"]["accuracy"] == 5
    assert scored["a"]["baseline"]["connection"] == 1


def test_score_non_interactive_includes_all_dimensions(tmp_path: Path) -> None:
    """All five dimensions are present in scored output for both configs."""
    from eval.score import score_paired

    default = tmp_path / "default.json"
    baseline = tmp_path / "baseline.json"
    _write_results(default, [{"case_id": "x", "config": "default", "reply": "ok"}])
    _write_results(baseline, [{"case_id": "x", "config": "baseline", "reply": "ok"}])

    prefill = tmp_path / "prefill.json"
    prefill.write_text(json.dumps({
        "x": {
            "default": {d: 5 for d in DIMENSIONS},
            "baseline": {d: 1 for d in DIMENSIONS},
        }
    }))

    out = tmp_path / "scored.json"
    score_paired(
        default_path=default,
        baseline_path=baseline,
        out_path=out,
        non_interactive_path=prefill,
    )
    scored = json.loads(out.read_text())
    for cfg in ("default", "baseline"):
        for dim in DIMENSIONS:
            assert dim in scored["x"][cfg]


def test_score_handles_multiple_cases_in_pair(tmp_path: Path) -> None:
    """Scoring two cases each pulls from the prefill file by case_id."""
    from eval.score import score_paired

    default = tmp_path / "default.json"
    baseline = tmp_path / "baseline.json"
    _write_results(default, [
        {"case_id": "a", "config": "default", "reply": "ok"},
        {"case_id": "b", "config": "default", "reply": "ok"},
    ])
    _write_results(baseline, [
        {"case_id": "a", "config": "baseline", "reply": "ok"},
        {"case_id": "b", "config": "baseline", "reply": "ok"},
    ])

    prefill = tmp_path / "prefill.json"
    prefill.write_text(json.dumps({
        "a": {
            "default": {d: 5 for d in DIMENSIONS},
            "baseline": {d: 2 for d in DIMENSIONS},
        },
        "b": {
            "default": {d: 4 for d in DIMENSIONS},
            "baseline": {d: 3 for d in DIMENSIONS},
        },
    }))

    out = tmp_path / "scored.json"
    score_paired(
        default_path=default,
        baseline_path=baseline,
        out_path=out,
        non_interactive_path=prefill,
    )
    scored = json.loads(out.read_text())
    assert set(scored.keys()) == {"a", "b"}
    assert scored["b"]["baseline"]["accuracy"] == 3


def test_score_missing_score_in_prefill_marks_case_skipped(tmp_path: Path) -> None:
    """If the prefill is missing a case's scores, the case is recorded as skipped (not crashed)."""
    from eval.score import score_paired

    default = tmp_path / "default.json"
    baseline = tmp_path / "baseline.json"
    _write_results(default, [{"case_id": "missing-1", "config": "default", "reply": "ok"}])
    _write_results(baseline, [{"case_id": "missing-1", "config": "baseline", "reply": "ok"}])

    prefill = tmp_path / "prefill.json"
    prefill.write_text(json.dumps({}))  # nothing for missing-1

    out = tmp_path / "scored.json"
    score_paired(
        default_path=default,
        baseline_path=baseline,
        out_path=out,
        non_interactive_path=prefill,
    )

    scored = json.loads(out.read_text())
    # Case is present but skipped (status field).
    assert scored["missing-1"]["_status"] == "skipped"


def test_score_invalid_likert_value_is_clamped_or_rejected(tmp_path: Path) -> None:
    """Scores outside 1..5 are rejected (raises ValueError on bad prefill)."""
    from eval.score import score_paired

    default = tmp_path / "default.json"
    baseline = tmp_path / "baseline.json"
    _write_results(default, [{"case_id": "bad-1", "config": "default", "reply": "ok"}])
    _write_results(baseline, [{"case_id": "bad-1", "config": "baseline", "reply": "ok"}])

    prefill = tmp_path / "prefill.json"
    prefill.write_text(json.dumps({
        "bad-1": {
            "default": {d: 99 for d in DIMENSIONS},  # invalid: > 5
            "baseline": {d: 1 for d in DIMENSIONS},
        }
    }))

    with pytest.raises(ValueError):
        score_paired(
            default_path=default,
            baseline_path=baseline,
            out_path=tmp_path / "scored.json",
            non_interactive_path=prefill,
        )


def test_score_judge_llm_flag_warns_v1_manual_only(tmp_path: Path, capsys) -> None:
    """``--judge llm`` prints a v1-not-supported warning and falls back to manual."""
    from eval.score import score_paired

    default = tmp_path / "default.json"
    baseline = tmp_path / "baseline.json"
    _write_results(default, [{"case_id": "j-1", "config": "default", "reply": "ok"}])
    _write_results(baseline, [{"case_id": "j-1", "config": "baseline", "reply": "ok"}])

    prefill = tmp_path / "prefill.json"
    prefill.write_text(json.dumps({
        "j-1": {
            "default": {d: 4 for d in DIMENSIONS},
            "baseline": {d: 2 for d in DIMENSIONS},
        }
    }))

    score_paired(
        default_path=default,
        baseline_path=baseline,
        out_path=tmp_path / "scored.json",
        non_interactive_path=prefill,
        judge="llm",
    )
    captured = capsys.readouterr()
    assert "v1" in captured.err.lower() or "v1" in captured.out.lower()
    assert "manual" in (captured.err + captured.out).lower()
