"""Fitness digest contributor — daily + weekly summary.

Per ``domain.yaml: digest``:

  - **daily.contents**: today's planned workout, calorie pacing,
    protein pacing, body metric alerts.
  - **weekly.contents**: weekly workout summary (sessions, volume,
    frequency), calorie avg vs target, macro avg vs target, body metric
    trend, compliance score, suggested actions.

When the relevant fitness data is missing, returns an empty string so
the digest assembler can omit the section cleanly.

The module is read-only — it never writes to the vault. Profile +
events are loaded fresh on every invocation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml

__all__ = ["summarize"]


_PROFILE_RELATIVE = Path("fitness") / "profile.yaml"
_WORKOUTS_RELATIVE = Path("fitness") / "workouts.jsonl"
_MEALS_RELATIVE = Path("fitness") / "meals.jsonl"
_METRICS_RELATIVE = Path("fitness") / "metrics.jsonl"
_PLANS_RELATIVE = Path("fitness") / "plans"

_WEEKLY_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------


def _load_profile(vault_root: Path) -> dict:
    """Load profile.yaml as a dict; return ``{}`` if missing/malformed."""
    path = vault_root / _PROFILE_RELATIVE
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _iter_jsonl(path: Path) -> Iterable[dict]:
    """Yield each row from the JSONL log; skip blank/bad lines."""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _coerce_dt(raw: object) -> Optional[datetime]:
    """Coerce a string / datetime into a tz-aware datetime, or None."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# daily
# ---------------------------------------------------------------------------


def _today_meals(vault_root: Path, now: datetime) -> list[dict]:
    """Return meals whose timestamp falls on the same date as ``now``."""
    today = now.date()
    today_meals: list[dict] = []
    for row in _iter_jsonl(vault_root / _MEALS_RELATIVE):
        ts = _coerce_dt(row.get("ts"))
        if ts is None:
            continue
        if ts.date() == today:
            today_meals.append(row)
    return today_meals


def _todays_planned_workout(vault_root: Path, now: datetime) -> Optional[Path]:
    """Find a plan markdown whose filename matches today's date."""
    plans_dir = vault_root / _PLANS_RELATIVE
    if not plans_dir.exists():
        return None
    today_iso = now.date().isoformat()
    for path in sorted(plans_dir.glob(f"{today_iso}-workout-*.md")):
        return path
    return None


def _format_daily(*, profile: dict, meals_today: list[dict], plan: Optional[Path]) -> str:
    """Render the daily section. Empty string when there's nothing to surface."""
    lines: list[str] = []

    if plan is not None:
        lines.append(f"- Today's planned workout: {plan.name}")

    target_kcal = profile.get("target_calories_kcal")
    target_protein = profile.get("target_protein_g")

    if meals_today:
        kcal_so_far = sum(
            float(m.get("total_kcal") or 0) for m in meals_today
        )
        protein_so_far = sum(
            float(m.get("total_protein_g") or 0) for m in meals_today
        )
        if target_kcal:
            lines.append(
                f"- Calories so far: {int(kcal_so_far)} / target {int(target_kcal)} kcal"
            )
        else:
            lines.append(f"- Calories so far: {int(kcal_so_far)} kcal")
        if target_protein:
            lines.append(
                f"- Protein so far: {int(protein_so_far)} / target {int(target_protein)} g"
            )
        else:
            lines.append(f"- Protein so far: {int(protein_so_far)} g")

    if not lines:
        return ""

    return "## Fitness: today\n\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# weekly
# ---------------------------------------------------------------------------


def _within_window(row: dict, *, since: datetime, key: str) -> bool:
    """Return True when ``row[key]`` parses to a datetime >= since."""
    ts = _coerce_dt(row.get(key))
    if ts is None:
        return False
    return ts >= since


def _weekly_workouts(vault_root: Path, since: datetime) -> list[dict]:
    """Return workouts whose ``date`` falls inside the weekly window."""
    rows: list[dict] = []
    for row in _iter_jsonl(vault_root / _WORKOUTS_RELATIVE):
        if _within_window(row, since=since, key="date"):
            rows.append(row)
    return rows


def _weekly_meals(vault_root: Path, since: datetime) -> list[dict]:
    """Return meals whose ``ts`` falls inside the weekly window."""
    rows: list[dict] = []
    for row in _iter_jsonl(vault_root / _MEALS_RELATIVE):
        if _within_window(row, since=since, key="ts"):
            rows.append(row)
    return rows


def _format_weekly(
    *,
    profile: dict,
    workouts: list[dict],
    meals: list[dict],
) -> str:
    """Render the weekly section. Empty string when there's nothing to surface."""
    lines: list[str] = []

    if workouts:
        total_minutes = sum(int(w.get("duration_min") or 0) for w in workouts)
        target_days = profile.get("weekly_training_days")
        target_clause = f" (target {target_days})" if target_days else ""
        lines.append(
            f"- Workouts: {len(workouts)} session(s), {total_minutes} min total{target_clause}"
        )

    if meals:
        days = {
            _coerce_dt(m.get("ts")).date() for m in meals if _coerce_dt(m.get("ts"))
        }
        n_days = max(1, len(days))
        avg_kcal = sum(float(m.get("total_kcal") or 0) for m in meals) / n_days
        avg_protein = sum(float(m.get("total_protein_g") or 0) for m in meals) / n_days
        target_kcal = profile.get("target_calories_kcal")
        target_protein = profile.get("target_protein_g")
        kcal_clause = (
            f"avg {int(avg_kcal)} / target {int(target_kcal)} kcal/day"
            if target_kcal
            else f"avg {int(avg_kcal)} kcal/day"
        )
        protein_clause = (
            f"avg {int(avg_protein)} / target {int(target_protein)} g/day"
            if target_protein
            else f"avg {int(avg_protein)} g/day"
        )
        lines.append(f"- Calories: {kcal_clause}")
        lines.append(f"- Protein:  {protein_clause}")

    if not lines:
        return ""

    return "## Fitness: this week\n\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def summarize(
    *,
    vault_root: str | os.PathLike[str],
    mode: str,
    now: Optional[datetime] = None,
) -> str:
    """Return the daily or weekly fitness digest section.

    Args:
        vault_root: vault root on disk; missing fitness files non-fatal.
        mode: ``daily`` or ``weekly``.
        now: reference time (defaults to current UTC).

    Returns:
        A markdown section, or empty string when there's nothing to report.

    Raises:
        ValueError: ``mode`` is not ``daily`` or ``weekly``.
    """
    if mode not in ("daily", "weekly"):
        raise ValueError(f"summarize mode must be daily|weekly, not {mode!r}")

    vault_path = Path(vault_root)
    now = now or datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    profile = _load_profile(vault_path)

    if mode == "daily":
        meals_today = _today_meals(vault_path, now)
        plan = _todays_planned_workout(vault_path, now)
        return _format_daily(profile=profile, meals_today=meals_today, plan=plan)

    since = now - timedelta(days=_WEEKLY_WINDOW_DAYS)
    workouts = _weekly_workouts(vault_path, since)
    meals = _weekly_meals(vault_path, since)
    return _format_weekly(profile=profile, workouts=workouts, meals=meals)
