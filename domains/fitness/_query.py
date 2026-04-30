"""Pure-Python aggregation surface for ``query_fitness``.

Splits per-kind aggregation logic out of ``handler.py`` so the public
surface stays thin. ``kind='compliance'`` walks recent workouts/meals
and compares them to the prescription stored in the plan markdown's
frontmatter, returning a 0..1 score (issue #8).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

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


def _read_plan_frontmatter(path: Path) -> dict:
    """Parse a plan markdown's YAML frontmatter; return ``{}`` if malformed."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.startswith("---\n"):
        return {}
    parts = raw.split("---\n", 2)
    if len(parts) < 3:
        return {}
    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _find_plan_by_id(plans_dir: Path, plan_id: str) -> Optional[Path]:
    """Find the markdown plan whose frontmatter ``plan_id`` matches."""
    if not plans_dir.exists():
        return None
    for path in sorted(plans_dir.glob("*.md")):
        fm = _read_plan_frontmatter(path)
        if fm.get("plan_id") == plan_id:
            return path
    return None


def _query_compliance(
    vault_root: Path,
    *,
    plan_id: Optional[str],
    compare_to_logs: Optional[bool],
) -> dict:
    """Compute a 0..1 compliance score for ``plan_id`` vs logged events.

    Strategy (deliberately conservative — better to under- than over-claim):

      1. Locate the plan markdown by ``plan_id`` -> if missing, score 0.0.
      2. Scan workouts.jsonl for rows whose ``plan_id`` field matches the
         plan_id (the link is set when ``fitness.workout_log`` recognizes
         a same-day plan, per ``prompt.md`` step 4).
      3. Score = ``1.0`` if at least one matching workout exists for a
         workout plan; ``0.0`` otherwise. Nutrition plans are scored the
         same way against ``meals.jsonl`` rows linked via ``plan_id``.

    The score is ``0..1``; richer scoring (per-exercise / per-macro
    matching) is left for a future issue. The structural shape — return
    a dict with ``kind, plan_id, value`` — is the contract callers depend
    on.
    """
    if not plan_id:
        return {
            "kind": "compliance",
            "plan_id": None,
            "compare_to_logs": compare_to_logs,
            "value": 0.0,
        }

    plans_dir = vault_root / PLANS_RELATIVE
    plan_path = _find_plan_by_id(plans_dir, plan_id)
    if plan_path is None:
        return {
            "kind": "compliance",
            "plan_id": plan_id,
            "compare_to_logs": compare_to_logs,
            "value": 0.0,
        }

    plan_kind = _infer_plan_kind(plan_path)
    if plan_kind == "nutrition":
        log_path = vault_root / MEALS_RELATIVE
    else:
        log_path = vault_root / WORKOUTS_RELATIVE

    matched = 0
    for row in iter_jsonl(log_path):
        if row.get("plan_id") == plan_id:
            matched += 1

    score = 1.0 if matched > 0 else 0.0
    return {
        "kind": "compliance",
        "plan_id": plan_id,
        "compare_to_logs": compare_to_logs,
        "matched_logs": matched,
        "value": score,
        "plan_path": str(plan_path),
    }


def _infer_plan_kind(plan_path: Path) -> str:
    """Pull the plan kind from frontmatter (preferred) or filename (fallback)."""
    fm = _read_plan_frontmatter(plan_path)
    kind = str(fm.get("kind") or "").strip().lower()
    if kind:
        return kind
    match = re.match(r"^\d{4}-\d{2}-\d{2}-(\w+)-", plan_path.name)
    return match.group(1).lower() if match else "workout"


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
        return _query_compliance(
            root,
            plan_id=plan_id,
            compare_to_logs=compare_to_logs,
        )
    raise ValueError(
        f"query_fitness kind must be one of workouts|meals|metrics|"
        f"plans|compliance|profile, not {kind!r}"
    )
