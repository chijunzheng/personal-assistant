"""Pure-Python aggregation surface for ``query_fitness``.

Splits per-kind aggregation logic out of ``handler.py`` so the public
surface stays thin. Plan generation (``kind='compliance'`` and richer
plan-side behaviour) lives in issue #8 — the ``compliance`` shape here
returns a structured "n/a" so callers don't crash.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from domains.fitness._io import iter_jsonl, load_yaml
from domains.fitness._paths import (
    MEALS_RELATIVE,
    METRICS_RELATIVE,
    PLANS_RELATIVE,
    PROFILE_RELATIVE,
    WORKOUTS_RELATIVE,
)

__all__ = ["query_fitness"]


def _date_in_range(
    candidate: Any,
    date_range: Optional[tuple[str, str]],
) -> bool:
    """True iff ``candidate`` (YYYY-MM-DD or ISO ts) falls in ``date_range``.

    A ``None`` range accepts all rows. Both endpoints are inclusive.
    """
    if not date_range:
        return True
    start, end = date_range
    head = str(candidate or "")[:10]  # YYYY-MM-DD prefix
    if start and head < str(start)[:10]:
        return False
    if end and head > str(end)[:10]:
        return False
    return True


def _query_workouts(
    vault_root: Path,
    *,
    date_range: Optional[tuple[str, str]],
    agg: Optional[str],
    workout_type: Optional[str],
) -> dict:
    """Aggregate workouts.jsonl by count / list / volume."""
    path = vault_root / WORKOUTS_RELATIVE
    rows = [
        r
        for r in iter_jsonl(path)
        if _date_in_range(r.get("date"), date_range)
        and (workout_type is None or r.get("type") == workout_type)
    ]
    if agg in (None, "count"):
        return {
            "kind": "workouts",
            "agg": "count",
            "date_range": date_range,
            "count": len(rows),
            "value": len(rows),
        }
    if agg == "list":
        return {
            "kind": "workouts",
            "agg": "list",
            "date_range": date_range,
            "count": len(rows),
            "rows": rows,
        }
    if agg == "volume":
        total = 0.0
        for r in rows:
            for ex in r.get("exercises") or []:
                sets = ex.get("sets") or 0
                reps = ex.get("reps") or 0
                weight = ex.get("weight_kg") or 0
                try:
                    total += float(sets) * float(reps) * float(weight)
                except (TypeError, ValueError):
                    continue
        return {
            "kind": "workouts",
            "agg": "volume",
            "date_range": date_range,
            "count": len(rows),
            "value": total,
        }
    raise ValueError(f"unsupported workouts agg {agg!r}")


def _query_meals(
    vault_root: Path,
    *,
    date_range: Optional[tuple[str, str]],
    agg: Optional[str],
    meal_type: Optional[str],
) -> dict:
    """Aggregate meals.jsonl by sum / avg / list."""
    path = vault_root / MEALS_RELATIVE
    rows = [
        r
        for r in iter_jsonl(path)
        if _date_in_range(r.get("ts"), date_range)
        and (meal_type is None or r.get("meal_type") == meal_type)
    ]
    if agg in (None, "sum"):
        totals = {
            "total_kcal": 0.0,
            "total_protein_g": 0.0,
            "total_carbs_g": 0.0,
            "total_fat_g": 0.0,
        }
        for r in rows:
            for k in totals:
                try:
                    totals[k] += float(r.get(k) or 0)
                except (TypeError, ValueError):
                    continue
        totals = {
            k: (int(v) if float(v).is_integer() else v) for k, v in totals.items()
        }
        return {
            "kind": "meals",
            "agg": "sum",
            "date_range": date_range,
            "count": len(rows),
            **totals,
        }
    if agg == "avg":
        if not rows:
            return {
                "kind": "meals",
                "agg": "avg",
                "date_range": date_range,
                "count": 0,
                "avg_kcal": 0.0,
                "avg_protein_g": 0.0,
            }
        avg_kcal = sum(float(r.get("total_kcal") or 0) for r in rows) / len(rows)
        avg_protein = sum(float(r.get("total_protein_g") or 0) for r in rows) / len(rows)
        return {
            "kind": "meals",
            "agg": "avg",
            "date_range": date_range,
            "count": len(rows),
            "avg_kcal": avg_kcal,
            "avg_protein_g": avg_protein,
        }
    if agg == "list":
        return {
            "kind": "meals",
            "agg": "list",
            "date_range": date_range,
            "count": len(rows),
            "rows": rows,
        }
    raise ValueError(f"unsupported meals agg {agg!r}")


def _query_metrics(
    vault_root: Path,
    *,
    metric_kind: Optional[str],
    date_range: Optional[tuple[str, str]],
    agg: Optional[str],
) -> dict:
    """Aggregate metrics.jsonl by trend / last / avg / list."""
    path = vault_root / METRICS_RELATIVE
    rows = [
        r
        for r in iter_jsonl(path)
        if (metric_kind is None or r.get("kind") == metric_kind)
        and _date_in_range(r.get("ts"), date_range)
    ]
    rows = sorted(rows, key=lambda r: str(r.get("ts") or ""))
    values = [float(r.get("value") or 0) for r in rows]

    if agg in (None, "trend"):
        if not values:
            return {
                "kind": "metrics",
                "agg": "trend",
                "metric_kind": metric_kind,
                "date_range": date_range,
                "count": 0,
                "avg": 0.0,
                "trend": 0.0,
            }
        avg = sum(values) / len(values)
        trend = (values[-1] - values[0]) / max(1, len(values) - 1)
        return {
            "kind": "metrics",
            "agg": "trend",
            "metric_kind": metric_kind,
            "date_range": date_range,
            "count": len(values),
            "avg": avg,
            "trend": trend,
        }
    if agg == "last":
        return {
            "kind": "metrics",
            "agg": "last",
            "metric_kind": metric_kind,
            "date_range": date_range,
            "count": len(values),
            "value": values[-1] if values else None,
        }
    if agg == "avg":
        return {
            "kind": "metrics",
            "agg": "avg",
            "metric_kind": metric_kind,
            "date_range": date_range,
            "count": len(values),
            "value": (sum(values) / len(values)) if values else 0.0,
        }
    if agg == "list":
        return {
            "kind": "metrics",
            "agg": "list",
            "metric_kind": metric_kind,
            "date_range": date_range,
            "count": len(values),
            "rows": rows,
        }
    raise ValueError(f"unsupported metrics agg {agg!r}")


def _query_plans(
    vault_root: Path,
    *,
    date_range: Optional[tuple[str, str]],
    plan_kind: Optional[str],
) -> dict:
    """List markdown plans under ``vault/fitness/plans/``.

    Plan generation lives in #8; this is a thin file lister so #7's
    ``query_fitness(kind='plans')`` returns coherent output if any plans
    happen to exist on disk.
    """
    plans_dir = vault_root / PLANS_RELATIVE
    if not plans_dir.exists():
        return {
            "kind": "plans",
            "date_range": date_range,
            "plan_kind": plan_kind,
            "count": 0,
            "rows": [],
        }
    entries: list[dict] = []
    for path in sorted(plans_dir.glob("*.md")):
        name = path.name
        if plan_kind and f"-{plan_kind}-" not in name:
            continue
        # Filename convention is "{date}-{kind}-{slug}.md".
        date_prefix = name[:10]
        if not _date_in_range(date_prefix, date_range):
            continue
        entries.append({"path": str(path), "name": name})
    return {
        "kind": "plans",
        "date_range": date_range,
        "plan_kind": plan_kind,
        "count": len(entries),
        "rows": entries,
    }


def _query_profile(vault_root: Path) -> dict:
    """Return the current profile.yaml (load-bearing for plan generation)."""
    profile = load_yaml(vault_root / PROFILE_RELATIVE)
    return {"kind": "profile", **profile}


def query_fitness(
    *,
    kind: str,
    vault_root: str,
    date_range: Optional[tuple[str, str]] = None,
    agg: Optional[str] = None,
    metric_kind: Optional[str] = None,
    workout_type: Optional[str] = None,
    meal_type: Optional[str] = None,
    plan_kind: Optional[str] = None,
    plan_id: Optional[str] = None,
    compare_to_logs: Optional[bool] = None,
) -> dict:
    """Pure-Python query over the fitness vault — never an LLM hand-sum.

    See ``domains/fitness/domain.yaml`` for the canonical signatures. The
    ``compliance`` shape is intentionally stubbed in #7; #8 fills it in
    once plan generation is on disk.

    Args:
        kind: ``workouts`` | ``meals`` | ``metrics`` | ``plans`` |
            ``compliance`` | ``profile``.
        vault_root: vault root on disk.
        date_range: ``(start_iso, end_iso)`` inclusive on both ends.
        agg: aggregation mode; per-kind shape varies.
        metric_kind: filter for ``kind='metrics'``.
        workout_type: filter for ``kind='workouts'``.
        meal_type: filter for ``kind='meals'``.
        plan_kind: filter for ``kind='plans'``.
        plan_id: pinpoint a plan for ``kind='compliance'``.
        compare_to_logs: requested comparison mode for compliance.

    Returns:
        A dict whose shape depends on ``kind`` + ``agg``.

    Raises:
        ValueError: ``kind`` is not one of the supported values.
    """
    root = Path(vault_root)
    if kind == "workouts":
        return _query_workouts(
            root,
            date_range=date_range,
            agg=agg,
            workout_type=workout_type,
        )
    if kind == "meals":
        return _query_meals(
            root,
            date_range=date_range,
            agg=agg,
            meal_type=meal_type,
        )
    if kind == "metrics":
        return _query_metrics(
            root,
            metric_kind=metric_kind,
            date_range=date_range,
            agg=agg,
        )
    if kind == "plans":
        return _query_plans(root, date_range=date_range, plan_kind=plan_kind)
    if kind == "profile":
        return _query_profile(root)
    if kind == "compliance":
        return {
            "kind": "compliance",
            "plan_id": plan_id,
            "compare_to_logs": compare_to_logs,
            "status": "n/a",
            "value": "n/a",
        }
    raise ValueError(
        f"query_fitness kind must be one of workouts|meals|metrics|"
        f"plans|compliance|profile, not {kind!r}"
    )
