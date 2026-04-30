"""Prompt + frontmatter assembly helpers for fitness plan generation.

Split out of ``_plans.py`` so the recipe orchestration stays focused on
flow (read inputs -> assemble prompt -> invoke -> persist) and the
text-shaping logic lives in its own small module.

These helpers are pure: they take the gathered inputs and return strings.
No I/O happens here, which keeps testing the recipe ergonomic — every
test seeds a temp vault then asserts on the prompt the LLM saw.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

__all__ = [
    "build_frontmatter",
    "build_nutrition_prompt",
    "build_workout_prompt",
    "next_plan_filename",
]


# ---------------------------------------------------------------------------
# Slug + filename
# ---------------------------------------------------------------------------


def _slug(text: str, *, fallback: str) -> str:
    """A short, file-safe slug derived from a plan body or fallback string."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:32] or fallback


def next_plan_filename(
    *,
    date_for: str,
    plan_kind: str,
    body: str,
) -> str:
    """Compute a stable, slug-suffixed plan filename.

    The slug is derived from the first line of the body (the plan title)
    so two distinct plans on the same day get distinct filenames; same
    inputs reproduce the same slug, keeping idempotency intact.
    """
    first_line = ""
    for line in body.splitlines():
        if line.strip().startswith("#"):
            first_line = line.strip("# ").strip()
            break
        if line.strip():
            first_line = line.strip()
            break
    slug = _slug(first_line, fallback=plan_kind)
    return f"{date_for}-{plan_kind}-{slug}.md"


# ---------------------------------------------------------------------------
# Brief formatters — one line per row, structured for grep-ability
# ---------------------------------------------------------------------------


def _format_workout_brief(rows: Iterable[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for r in rows:
        lines.append(
            "- workout-id:{id} date:{date} type:{type} intensity:{intensity} "
            "tags:{tags}".format(
                id=r.get("id", "?"),
                date=str(r.get("date", "?"))[:10],
                type=r.get("type", "?"),
                intensity=r.get("intensity", "?"),
                tags=",".join(r.get("tags") or []) or "-",
            )
        )
        for ex in (r.get("exercises") or [])[:6]:
            lines.append(
                "    - {name} sets={sets} reps={reps} weight_kg={weight}".format(
                    name=ex.get("name", "?"),
                    sets=ex.get("sets"),
                    reps=ex.get("reps"),
                    weight=ex.get("weight_kg"),
                )
            )
    return "\n".join(lines) or "- (no recent workouts)"


def _format_metric_brief(rows: Iterable[Mapping[str, Any]]) -> str:
    lines = []
    for r in rows:
        lines.append(
            "- metric-id:{id} ts:{ts} kind:{kind} value:{value}".format(
                id=r.get("id", "?"),
                ts=str(r.get("ts", "?"))[:10],
                kind=r.get("kind", "?"),
                value=r.get("value", "?"),
            )
        )
    return "\n".join(lines) or "- (no recent metrics)"


def _format_meal_brief(rows: Iterable[Mapping[str, Any]]) -> str:
    lines = []
    for r in rows:
        lines.append(
            "- meal-id:{id} ts:{ts} type:{mtype} kcal:{k} protein_g:{p}".format(
                id=r.get("id", "?"),
                ts=str(r.get("ts", "?"))[:10],
                mtype=r.get("meal_type", "?"),
                k=r.get("total_kcal", "?"),
                p=r.get("total_protein_g", "?"),
            )
        )
    return "\n".join(lines) or "- (no recent meals)"


def _format_journal_brief(
    paths: Iterable[Path],
    *,
    vault_root: Path,
) -> str:
    lines = []
    for path in paths:
        try:
            rel = path.relative_to(vault_root)
        except ValueError:
            rel = path
        snippet = ""
        try:
            snippet = path.read_text(encoding="utf-8")[:300].replace("\n", " ")
        except (OSError, UnicodeDecodeError):
            snippet = ""
        lines.append(f"- journal-path:{rel} excerpt:{snippet}")
    return "\n".join(lines) or "- (no journal cross-refs)"


def _format_inventory_brief(items: Iterable[Mapping[str, Any]]) -> str:
    lines = []
    for entry in items:
        tags = ",".join(entry.get("tags") or []) or "-"
        lines.append(
            "- {name} qty:{q} unit:{u} tags:{tags}".format(
                name=entry["item"],
                q=entry.get("quantity"),
                u=entry.get("unit"),
                tags=tags,
            )
        )
    return "\n".join(lines) or "- (inventory empty)"


def _last_plan_section(
    last_plan: Optional[Mapping[str, Any]],
    *,
    vault_root: Path,
) -> str:
    if not last_plan or not last_plan.get("frontmatter"):
        return "(none)"
    fm = last_plan["frontmatter"]
    try:
        rel = last_plan["path"].relative_to(vault_root)
    except (ValueError, AttributeError):
        rel = last_plan["path"]
    return f"path:{rel} plan_id:{fm.get('plan_id')}"


# ---------------------------------------------------------------------------
# Whole-prompt builders
# ---------------------------------------------------------------------------


def build_workout_prompt(
    *,
    profile: Mapping[str, Any],
    workouts: list[dict],
    metrics: list[dict],
    journal_paths: list[Path],
    last_plan: Optional[Mapping[str, Any]],
    vault_root: Path,
    date_for: str,
    workout_lookback_days: int,
    metric_lookback_days: int,
    recovery_metric_lookback_days: int,
    journal_lookback_days: int,
    last_plan_lookback_days: int,
) -> str:
    """Assemble the workout-plan prompt fed to ``claude_runner.invoke``."""
    profile_yaml = yaml.safe_dump(dict(profile), sort_keys=True).strip()
    return (
        f"Generate a workout plan for {date_for}.\n\n"
        f"## Profile (vault/fitness/profile.yaml)\n{profile_yaml}\n\n"
        f"## Recent workouts (last {workout_lookback_days} days)\n"
        f"{_format_workout_brief(workouts)}\n\n"
        f"## Recent metrics (last {metric_lookback_days} days, recovery markers in last "
        f"{recovery_metric_lookback_days})\n{_format_metric_brief(metrics)}\n\n"
        f"## Journal cross-refs (last {journal_lookback_days} days)\n"
        f"{_format_journal_brief(journal_paths, vault_root=vault_root)}\n\n"
        f"## Last similar plan (last {last_plan_lookback_days} days)\n"
        f"{_last_plan_section(last_plan, vault_root=vault_root)}\n\n"
        "Generate a markdown workout plan body (no frontmatter — handler "
        "will add). Cite at least one piece of recent context by id or "
        "filename. If recovery is poor (sleep <6h or journal mentions "
        "exhaustion/illness), recommend a moderate or recovery session "
        "instead of a max effort.\n"
    )


def build_nutrition_prompt(
    *,
    profile: Mapping[str, Any],
    meals: list[dict],
    weight_metrics: list[dict],
    inventory_items: list[dict],
    journal_paths: list[Path],
    last_plan: Optional[Mapping[str, Any]],
    vault_root: Path,
    date_for: str,
    meal_lookback_days: int,
    weight_lookback_days: int,
    journal_lookback_days: int,
    last_plan_lookback_days: int,
) -> str:
    """Assemble the nutrition-plan prompt fed to ``claude_runner.invoke``."""
    profile_yaml = yaml.safe_dump(dict(profile), sort_keys=True).strip()
    restrictions = ", ".join(profile.get("dietary_restrictions") or []) or "(none)"
    target_kcal = profile.get("target_calories_kcal")
    target_protein = profile.get("target_protein_g")
    return (
        f"Generate a nutrition plan for {date_for}.\n\n"
        f"## Profile (target_calories_kcal={target_kcal}, "
        f"target_protein_g={target_protein}, restrictions={restrictions})\n"
        f"{profile_yaml}\n\n"
        f"## Recent meals (last {meal_lookback_days} days)\n"
        f"{_format_meal_brief(meals)}\n\n"
        f"## Weight trend (last {weight_lookback_days} days)\n"
        f"{_format_metric_brief(weight_metrics)}\n\n"
        f"## Inventory (vault/inventory/state.yaml)\n"
        f"{_format_inventory_brief(inventory_items)}\n\n"
        f"## Journal cross-refs (last {journal_lookback_days} days)\n"
        f"{_format_journal_brief(journal_paths, vault_root=vault_root)}\n\n"
        f"## Last similar plan (last {last_plan_lookback_days} days)\n"
        f"{_last_plan_section(last_plan, vault_root=vault_root)}\n\n"
        "Generate a markdown nutrition plan body (no frontmatter). Hit "
        f"{target_kcal} kcal and at least {target_protein}g protein. Use ONLY "
        "items present in the inventory listing above. Strictly avoid any "
        f"foods that violate the dietary restrictions ({restrictions}).\n"
    )


# ---------------------------------------------------------------------------
# Frontmatter assembly
# ---------------------------------------------------------------------------


def build_frontmatter(
    *,
    plan_id: str,
    kind: str,
    date_generated: str,
    date_for: str,
    profile_sha: str,
    recent_workouts: list[str],
    recent_metrics: list[str],
    recent_meals: list[str],
    journal_cross_refs: list[str],
    last_plan_id: Optional[str],
    tags: list[str],
) -> str:
    """Render the YAML frontmatter the handler prepends to the plan body."""
    payload = {
        "plan_id": plan_id,
        "kind": kind,
        "date_generated": date_generated,
        "date_for": date_for,
        "based_on": {
            "profile_snapshot_sha256": profile_sha,
            "recent_workouts": recent_workouts,
            "recent_metrics": recent_metrics,
            "recent_meals": recent_meals,
            "journal_cross_refs": journal_cross_refs,
            "last_plan_id": last_plan_id,
        },
        "tags": tags,
    }
    return "---\n" + yaml.safe_dump(payload, sort_keys=True) + "---\n"
