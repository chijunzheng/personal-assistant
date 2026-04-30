"""Compose ``docs/eval-progression.md`` from paired results + scored data.

The report is the human-readable artifact for the portfolio: it shows
the engineered config (default) head-to-head against the vanilla baseline
on five Likert dimensions, on token budget, and on the tool palette.

Sections rendered (in this order):

  1. **Header** — timestamp + which result files were composed
  2. **5-dimension scoring** (only if a scored file is provided) —
     mean + median per dimension, default vs baseline
  3. **Token chart** — per-case token totals + aggregate (mean / median / p95)
  4. **Tool-palette delta** — set of tools each config invoked, plus the
     symmetric difference (default-only / baseline-only / common)

All tables are sorted by ``case_id`` so diffs across runs are stable.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "DIMENSIONS",
    "compose_report",
    "main",
]


DIMENSIONS: tuple[str, ...] = (
    "accuracy",
    "grounding",
    "conciseness",
    "connection",
    "trust",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    """Read a JSON file with a clear failure message."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise ValueError(f"could not load {path}: {err}") from err


def _index_by_case_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index a result-file row list by ``case_id``."""
    return {r.get("case_id"): r for r in rows if r.get("case_id") is not None}


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile in [0, 100]; handles tiny inputs."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (q / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return float(sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * weight)


def _aggregate_tokens(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Compute mean / median / p95 over the ``total_tokens`` series."""
    series = [int(r.get("total_tokens", 0)) for r in rows]
    if not series:
        return {"mean": 0.0, "median": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "mean": float(statistics.fmean(series)),
        "median": float(statistics.median(series)),
        "p95": _percentile([float(v) for v in series], 95.0),
        "max": float(max(series)),
    }


def _aggregate_dim(scored: dict[str, Any], config: str, dim: str) -> dict[str, float]:
    """Mean + median for one (config, dim) pair across all OK-scored cases."""
    values: list[float] = []
    for case_id, block in scored.items():
        if not isinstance(block, dict):
            continue
        if block.get("_status") != "ok":
            continue
        cfg_block = block.get(config) or {}
        if dim in cfg_block:
            try:
                values.append(float(cfg_block[dim]))
            except (TypeError, ValueError):
                continue
    if not values:
        return {"mean": 0.0, "median": 0.0, "n": 0}
    return {
        "mean": float(statistics.fmean(values)),
        "median": float(statistics.median(values)),
        "n": len(values),
    }


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_header(default_path: Path, baseline_path: Path, scored_path: Path | None) -> str:
    """Top-of-report banner — sources + timestamp."""
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Eval progression",
        "",
        f"_generated: {now}_",
        "",
        f"- default: `{default_path}`",
        f"- baseline: `{baseline_path}`",
    ]
    if scored_path is not None:
        lines.append(f"- scored: `{scored_path}`")
    lines.append("")
    return "\n".join(lines)


def _render_five_dim_table(scored: dict[str, Any]) -> str:
    """Markdown table: rows = dimensions, columns = mean/median for each config."""
    out: list[str] = ["## 5-dimension scoring", ""]
    out.append("| dimension | default mean | default median | baseline mean | baseline median | Δ mean |")
    out.append("|---|---:|---:|---:|---:|---:|")
    for dim in DIMENSIONS:
        d = _aggregate_dim(scored, "default", dim)
        b = _aggregate_dim(scored, "baseline", dim)
        delta = d["mean"] - b["mean"]
        out.append(
            f"| {dim} | {d['mean']:.2f} | {d['median']:.2f} "
            f"| {b['mean']:.2f} | {b['median']:.2f} | {delta:+.2f} |"
        )
    out.append("")
    return "\n".join(out)


def _render_token_chart(
    default_by_id: dict[str, dict[str, Any]],
    baseline_by_id: dict[str, dict[str, Any]],
) -> str:
    """Per-case token table + aggregate row at the bottom."""
    out: list[str] = ["## Token chart", ""]
    out.append("| case_id | default tokens | baseline tokens | Δ tokens |")
    out.append("|---|---:|---:|---:|")
    case_ids = sorted(set(default_by_id) | set(baseline_by_id))
    default_series: list[int] = []
    baseline_series: list[int] = []
    for cid in case_ids:
        d = int(default_by_id.get(cid, {}).get("total_tokens", 0))
        b = int(baseline_by_id.get(cid, {}).get("total_tokens", 0))
        default_series.append(d)
        baseline_series.append(b)
        out.append(f"| {cid} | {d} | {b} | {d - b:+d} |")
    out.append("")

    # Aggregate stats below the per-case table.
    def _agg(series: list[int]) -> dict[str, float]:
        return _aggregate_tokens(
            [{"total_tokens": v} for v in series]
        )

    d_agg = _agg(default_series)
    b_agg = _agg(baseline_series)
    out.append("| stat | default | baseline | Δ |")
    out.append("|---|---:|---:|---:|")
    for stat in ("mean", "median", "p95"):
        out.append(
            f"| {stat} | {d_agg[stat]:.1f} | {b_agg[stat]:.1f} | "
            f"{d_agg[stat] - b_agg[stat]:+.1f} |"
        )
    out.append("")
    return "\n".join(out)


def _render_tool_palette_delta(
    default_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
) -> str:
    """Set difference of tools used per config (lifted from each row's ``tool_calls``)."""
    def _tool_set(rows: list[dict[str, Any]]) -> set[str]:
        used: set[str] = set()
        for r in rows:
            for t in r.get("tool_calls") or []:
                used.add(str(t))
        return used

    d_set = _tool_set(default_rows)
    b_set = _tool_set(baseline_rows)
    common = d_set & b_set
    only_default = d_set - b_set
    only_baseline = b_set - d_set

    out: list[str] = ["## Tool-palette delta", ""]
    out.append("| group | tools |")
    out.append("|---|---|")
    out.append(f"| common | {', '.join(sorted(common)) or '—'} |")
    out.append(f"| default only | {', '.join(sorted(only_default)) or '—'} |")
    out.append(f"| baseline only | {', '.join(sorted(only_baseline)) or '—'} |")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def compose_report(
    *,
    default_path: str | os.PathLike[str],
    baseline_path: str | os.PathLike[str],
    out_path: str | os.PathLike[str],
    scored_path: str | os.PathLike[str] | None = None,
) -> Path:
    """Assemble the full markdown report and write it to ``out_path``."""
    default_p = Path(default_path)
    baseline_p = Path(baseline_path)
    scored_p = Path(scored_path) if scored_path is not None else None

    default_rows = _load_json(default_p)
    baseline_rows = _load_json(baseline_p)
    if not isinstance(default_rows, list) or not isinstance(baseline_rows, list):
        raise ValueError("paired result files must be JSON arrays of row dicts")

    default_by_id = _index_by_case_id(default_rows)
    baseline_by_id = _index_by_case_id(baseline_rows)

    parts: list[str] = [_render_header(default_p, baseline_p, scored_p)]

    if scored_p is not None and scored_p.exists():
        scored = _load_json(scored_p)
        if not isinstance(scored, dict):
            raise ValueError("scored file must be a JSON object keyed by case_id")
        parts.append(_render_five_dim_table(scored))

    parts.append(_render_token_chart(default_by_id, baseline_by_id))
    parts.append(_render_tool_palette_delta(default_rows, baseline_rows))

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(parts), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval.report",
        description="Compose docs/eval-progression.md from paired result files.",
    )
    parser.add_argument(
        "--paired",
        nargs=2,
        required=True,
        metavar=("DEFAULT_JSON", "BASELINE_JSON"),
        help="Paths to the default + baseline results JSON files.",
    )
    parser.add_argument(
        "--scored",
        default=None,
        help="Optional path to scored JSON for the 5-dim section.",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Where to write the markdown report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    default_path, baseline_path = args.paired
    compose_report(
        default_path=default_path,
        baseline_path=baseline_path,
        scored_path=args.scored,
        out_path=args.out,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
