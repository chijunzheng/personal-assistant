"""Adaptive plan generation — workout + nutrition (issue #8).

Implements the 7-step recipe from ``domains/fitness/prompt.md`` for both
``fitness.workout_plan`` and ``fitness.nutrition_plan`` intents.

Discipline:

  - Plugin isolation. We do NOT import ``domains.journal`` or
    ``domains.inventory`` — cross-domain reads happen via filesystem
    primitives only (``Path.read_text`` and ``rglob``).
  - All vault writes route through ``kernel.vault.atomic_write`` so a
    Drive sync mid-write never observes a half-written file.
  - Idempotent on a content-derived ``plan_id`` (sha256 of the body): the
    same inputs + clock yield the same plan_id, so re-running on the same
    day overwrites the file in place rather than appending duplicates.

Prompt + frontmatter shaping lives in ``_plan_prompts.py`` so this file
stays focused on the recipe flow (read inputs -> assemble prompt ->
invoke -> persist).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol

import yaml

from kernel.claude_runner import ClaudeResponse
from kernel.vault import atomic_write

from domains.fitness._io import iter_jsonl, load_yaml
from domains.fitness._paths import (
    METRICS_RELATIVE,
    PLANS_RELATIVE,
    PROFILE_RELATIVE,
    WORKOUTS_RELATIVE,
)
from domains.fitness._plan_prompts import (
    build_frontmatter,
    build_nutrition_prompt,
    build_workout_prompt,
    next_plan_filename,
)
from domains.fitness._query import query_fitness

__all__ = [
    "PLAN_INTENTS",
    "generate_plan",
    "is_profile_filled",
    "load_journal_cross_refs",
    "next_plan_filename",
    "query_fitness",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Plan-generation intents handled by this module.
PLAN_INTENTS = ("fitness.workout_plan", "fitness.nutrition_plan")

# How far back the recipe consults each input.
_WORKOUT_LOOKBACK_DAYS = 14
_METRIC_LOOKBACK_DAYS = 28
_RECOVERY_METRIC_LOOKBACK_DAYS = 7
_JOURNAL_LOOKBACK_DAYS = 14
_LAST_PLAN_LOOKBACK_DAYS = 7
_NUTRITION_MEAL_LOOKBACK_DAYS = 7
_NUTRITION_WEIGHT_LOOKBACK_DAYS = 14

# Subjective-signal keywords searched for in journal entries.
_JOURNAL_KEYWORDS = (
    "exhaust",
    "tired",
    "fatigue",
    "sore",
    "soreness",
    "energy",
    "sleep",
    "couldn't sleep",
    "stress",
    "back twinge",
    "twinge",
    "vacation",
    "sick",
    "ill",
    "injury",
    "hunger",
    "hungry",
    "satiety",
    "dinner",
    "social",
)

# What we consider "TODO" placeholders in profile.yaml.
_TODO_TOKENS = ("TODO", "todo", "To-Do")

# Required profile fields for a workout plan (what the recipe demands).
_REQUIRED_WORKOUT_FIELDS = (
    "sex",
    "weight_kg",
    "height_cm",
    "goal",
    "weekly_training_days",
    "equipment_available",
)

# Required profile fields for a nutrition plan.
_REQUIRED_NUTRITION_FIELDS = (
    "sex",
    "weight_kg",
    "height_cm",
    "goal",
    "dietary_restrictions",
)


# ---------------------------------------------------------------------------
# Public protocols
# ---------------------------------------------------------------------------


class _ClaudeInvoker(Protocol):
    """The subset of ``claude_runner.invoke`` plan generation uses."""

    def __call__(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> ClaudeResponse: ...


# ---------------------------------------------------------------------------
# Profile fitness check (TODO refusal)
# ---------------------------------------------------------------------------


def _value_is_todo(value: Any) -> bool:
    """True iff ``value`` is a TODO placeholder (string match) or list-of-TODO."""
    if isinstance(value, str):
        return value.strip() in _TODO_TOKENS
    if isinstance(value, (list, tuple)):
        return any(isinstance(v, str) and v.strip() in _TODO_TOKENS for v in value)
    return False


def is_profile_filled(
    profile: Mapping[str, Any],
    *,
    required: Iterable[str],
) -> tuple[bool, list[str]]:
    """Return ``(ok, missing_fields)`` for the required field set.

    A field is "missing" when:

      - the field is absent
      - the value is ``None``
      - the value is the literal string ``"TODO"`` (case-insensitive)
    """
    missing: list[str] = []
    for field in required:
        if field not in profile:
            missing.append(field)
            continue
        value = profile.get(field)
        if value is None:
            missing.append(field)
            continue
        if _value_is_todo(value):
            missing.append(field)
            continue
    return (not missing, missing)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _date_iso(d: datetime) -> str:
    return d.date().isoformat()


def _date_range(now: datetime, lookback_days: int) -> tuple[str, str]:
    """Inclusive ``(start, end)`` ISO-date strings for ``[now - N, now]``."""
    end = now
    start = now - timedelta(days=lookback_days)
    return (_date_iso(start), _date_iso(end))


# ---------------------------------------------------------------------------
# Cross-domain journal grep (no journal-handler import)
# ---------------------------------------------------------------------------


def _journal_entry_in_range(name: str, *, start: str, end: str) -> bool:
    """Heuristic: filenames that begin with ``YYYY-MM-DD`` are date-windowed."""
    head = name[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", head):
        # No date prefix — accept and let content-grep decide.
        return True
    return start <= head <= end


def load_journal_cross_refs(
    *,
    vault_root: Path,
    now: datetime,
    lookback_days: int = _JOURNAL_LOOKBACK_DAYS,
    keywords: Iterable[str] = _JOURNAL_KEYWORDS,
) -> list[Path]:
    """Walk ``vault/journal/`` and return paths matching fitness-relevant keywords.

    Cross-domain read implemented purely via the filesystem; we never
    import the journal plugin. The caller decides what to do with the
    returned paths (typically: include them in the prompt so the LLM can
    quote them in the plan body).
    """
    journal_root = vault_root / "journal"
    if not journal_root.exists():
        return []

    start, end = _date_range(now, lookback_days)
    needles = tuple(k.lower() for k in keywords)
    matches: list[Path] = []
    for path in sorted(journal_root.rglob("*.md")):
        if not _journal_entry_in_range(path.name, start=start, end=end):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        haystack = text.lower()
        if any(needle in haystack for needle in needles):
            matches.append(path)
    return matches


# ---------------------------------------------------------------------------
# Cross-domain inventory read (no inventory-handler import)
# ---------------------------------------------------------------------------


def _load_inventory_state(vault_root: Path) -> dict:
    """Read ``vault/inventory/state.yaml`` directly. Returns ``{}`` if missing."""
    path = vault_root / "inventory" / "state.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _inventory_items(state: Mapping[str, Any]) -> list[dict]:
    """Coerce state.yaml shapes into a list of ``{item, quantity, unit, tags}`` dicts.

    Inventory state files can be shaped as either:

      - ``{name: {quantity, unit, tags?}}`` (current production shape)
      - ``{items: [{item, quantity, unit, tags?}]}`` (alternative seen in
        eval seed data)

    We tolerate both so eval cases written against the second shape work
    against the production handler unchanged.
    """
    if isinstance(state.get("items"), list):
        out: list[dict] = []
        for entry in state["items"]:
            if not isinstance(entry, Mapping):
                continue
            out.append(
                {
                    "item": str(entry.get("item") or entry.get("name") or "").strip(),
                    "quantity": entry.get("quantity"),
                    "unit": entry.get("unit"),
                    "tags": list(entry.get("tags") or []),
                }
            )
        return [e for e in out if e["item"]]

    out = []
    for name, body in state.items():
        if not isinstance(body, Mapping):
            continue
        out.append(
            {
                "item": str(name).strip(),
                "quantity": body.get("quantity"),
                "unit": body.get("unit"),
                "tags": list(body.get("tags") or []),
            }
        )
    return [e for e in out if e["item"]]


# ---------------------------------------------------------------------------
# Recent inputs (workouts / metrics / meals / last plan)
# ---------------------------------------------------------------------------


def _recent_workouts(vault_root: Path, now: datetime) -> list[dict]:
    start, end = _date_range(now, _WORKOUT_LOOKBACK_DAYS)
    rows: list[dict] = []
    for row in iter_jsonl(vault_root / WORKOUTS_RELATIVE):
        date = str(row.get("date") or "")[:10]
        if start <= date <= end:
            rows.append(row)
    return rows


def _recent_metrics(vault_root: Path, now: datetime) -> list[dict]:
    """Return metrics from a 28-day window (recovery markers in the last 7)."""
    start, end = _date_range(now, _METRIC_LOOKBACK_DAYS)
    rows: list[dict] = []
    for row in iter_jsonl(vault_root / METRICS_RELATIVE):
        ts = str(row.get("ts") or "")[:10]
        if start <= ts <= end:
            rows.append(row)
    return rows


def _recent_meals(vault_root: Path, now: datetime) -> list[dict]:
    start, end = _date_range(now, _NUTRITION_MEAL_LOOKBACK_DAYS)
    rows: list[dict] = []
    for row in iter_jsonl(vault_root / "fitness" / "meals.jsonl"):
        ts = str(row.get("ts") or "")[:10]
        if start <= ts <= end:
            rows.append(row)
    return rows


def _last_plan(
    vault_root: Path,
    now: datetime,
    *,
    plan_kind: str,
) -> Optional[dict]:
    """Return the most recent plan of the given kind within 7 days, or None."""
    plans_dir = vault_root / PLANS_RELATIVE
    if not plans_dir.exists():
        return None
    start, end = _date_range(now, _LAST_PLAN_LOOKBACK_DAYS)
    candidates = []
    for path in plans_dir.glob("*.md"):
        if f"-{plan_kind}-" not in path.name:
            continue
        head = path.name[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", head):
            continue
        if not (start <= head <= end):
            continue
        candidates.append((head, path))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    _, latest = candidates[0]
    fm = _parse_frontmatter(latest)
    return {"path": latest, "frontmatter": fm}


def _parse_frontmatter(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
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


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_text(path.read_text(encoding="utf-8")) if path.exists() else ""


def _journal_paths_relative(paths: Iterable[Path], *, vault_root: Path) -> list[str]:
    out: list[str] = []
    for p in paths:
        try:
            out.append(str(p.relative_to(vault_root)))
        except ValueError:
            out.append(str(p))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate_plan(
    *,
    intent: str,
    query: str,  # noqa: ARG001 — preserved for future routing logic
    vault_root: Path,
    invoker: _ClaudeInvoker,
    clock: Optional[callable] = None,
) -> dict:
    """Run the 7-step recipe, save the plan, return a dispatch summary.

    Returns a dict shaped:

      {
        "ok": bool,
        "reply": str,            # the message the orchestrator hands back
        "path": Optional[Path],  # where the plan landed (if any)
        "plan_id": Optional[str],
      }
    """
    if intent not in PLAN_INTENTS:
        raise ValueError(
            f"generate_plan only handles {PLAN_INTENTS}, not {intent!r}"
        )

    now = (clock or (lambda: datetime.now(tz=timezone.utc)))()
    plan_kind = "workout" if intent == "fitness.workout_plan" else "nutrition"

    # Step 1 — Profile (refusal guard).
    profile_path = vault_root / PROFILE_RELATIVE
    profile = load_yaml(profile_path)
    required = (
        _REQUIRED_WORKOUT_FIELDS
        if intent == "fitness.workout_plan"
        else _REQUIRED_NUTRITION_FIELDS
    )
    ok, missing = is_profile_filled(profile, required=required)
    if not ok:
        return {
            "ok": False,
            "reply": (
                "Cannot generate a plan — your fitness profile still has "
                f"TODO holes ({', '.join(missing)}). Please fill in "
                f"vault/fitness/profile.yaml before requesting a plan."
            ),
            "path": None,
            "plan_id": None,
        }

    profile_sha = _sha256_file(profile_path)

    if intent == "fitness.workout_plan":
        return _generate_workout(
            vault_root=vault_root,
            invoker=invoker,
            now=now,
            plan_kind=plan_kind,
            profile=profile,
            profile_sha=profile_sha,
        )
    return _generate_nutrition(
        vault_root=vault_root,
        invoker=invoker,
        now=now,
        plan_kind=plan_kind,
        profile=profile,
        profile_sha=profile_sha,
    )


# ---------------------------------------------------------------------------
# Internal: workout-specific recipe
# ---------------------------------------------------------------------------


def _generate_workout(
    *,
    vault_root: Path,
    invoker: _ClaudeInvoker,
    now: datetime,
    plan_kind: str,
    profile: Mapping[str, Any],
    profile_sha: str,
) -> dict:
    workouts = _recent_workouts(vault_root, now)
    metrics = _recent_metrics(vault_root, now)
    journal_paths = load_journal_cross_refs(vault_root=vault_root, now=now)
    last_plan = _last_plan(vault_root, now, plan_kind=plan_kind)
    date_for = _date_iso(now)

    prompt = build_workout_prompt(
        profile=profile,
        workouts=workouts,
        metrics=metrics,
        journal_paths=journal_paths,
        last_plan=last_plan,
        vault_root=vault_root,
        date_for=date_for,
        workout_lookback_days=_WORKOUT_LOOKBACK_DAYS,
        metric_lookback_days=_METRIC_LOOKBACK_DAYS,
        recovery_metric_lookback_days=_RECOVERY_METRIC_LOOKBACK_DAYS,
        journal_lookback_days=_JOURNAL_LOOKBACK_DAYS,
        last_plan_lookback_days=_LAST_PLAN_LOOKBACK_DAYS,
    )

    response = invoker(prompt, system_prompt=None)
    body = response.text.strip() + "\n"

    plan_id = _sha256_text(body)
    last_plan_id = (
        last_plan["frontmatter"].get("plan_id")
        if last_plan and last_plan.get("frontmatter")
        else None
    )
    frontmatter = build_frontmatter(
        plan_id=plan_id,
        kind=plan_kind,
        date_generated=now.isoformat(),
        date_for=date_for,
        profile_sha=profile_sha,
        recent_workouts=[str(w.get("id", "")) for w in workouts if w.get("id")],
        recent_metrics=[str(m.get("id", "")) for m in metrics if m.get("id")],
        recent_meals=[],
        journal_cross_refs=_journal_paths_relative(journal_paths, vault_root=vault_root),
        last_plan_id=last_plan_id,
        tags=[plan_kind],
    )

    plan_path = _persist_plan(
        vault_root=vault_root,
        date_for=date_for,
        plan_kind=plan_kind,
        frontmatter=frontmatter,
        body=body,
    )
    return {
        "ok": True,
        "reply": f"Saved plan to fitness/plans/{plan_path.name}.",
        "path": plan_path,
        "plan_id": plan_id,
    }


# ---------------------------------------------------------------------------
# Internal: nutrition-specific recipe
# ---------------------------------------------------------------------------


def _generate_nutrition(
    *,
    vault_root: Path,
    invoker: _ClaudeInvoker,
    now: datetime,
    plan_kind: str,
    profile: Mapping[str, Any],
    profile_sha: str,
) -> dict:
    meals = _recent_meals(vault_root, now)
    metric_rows = _recent_metrics(vault_root, now)
    weight_window_start, _ = _date_range(now, _NUTRITION_WEIGHT_LOOKBACK_DAYS)
    weight_metrics = [
        m
        for m in metric_rows
        if str(m.get("kind") or "").lower() == "weight"
        and str(m.get("ts") or "")[:10] >= weight_window_start
    ]
    inventory = _inventory_items(_load_inventory_state(vault_root))
    journal_paths = load_journal_cross_refs(vault_root=vault_root, now=now)
    last_plan = _last_plan(vault_root, now, plan_kind=plan_kind)
    date_for = _date_iso(now)

    prompt = build_nutrition_prompt(
        profile=profile,
        meals=meals,
        weight_metrics=weight_metrics,
        inventory_items=inventory,
        journal_paths=journal_paths,
        last_plan=last_plan,
        vault_root=vault_root,
        date_for=date_for,
        meal_lookback_days=_NUTRITION_MEAL_LOOKBACK_DAYS,
        weight_lookback_days=_NUTRITION_WEIGHT_LOOKBACK_DAYS,
        journal_lookback_days=_JOURNAL_LOOKBACK_DAYS,
        last_plan_lookback_days=_LAST_PLAN_LOOKBACK_DAYS,
    )

    response = invoker(prompt, system_prompt=None)
    body = response.text.strip() + "\n"

    plan_id = _sha256_text(body)
    last_plan_id = (
        last_plan["frontmatter"].get("plan_id")
        if last_plan and last_plan.get("frontmatter")
        else None
    )
    frontmatter = build_frontmatter(
        plan_id=plan_id,
        kind=plan_kind,
        date_generated=now.isoformat(),
        date_for=date_for,
        profile_sha=profile_sha,
        recent_workouts=[],
        recent_metrics=[str(m.get("id", "")) for m in weight_metrics if m.get("id")],
        recent_meals=[str(m.get("id", "")) for m in meals if m.get("id")],
        journal_cross_refs=_journal_paths_relative(journal_paths, vault_root=vault_root),
        last_plan_id=last_plan_id,
        tags=[plan_kind],
    )

    plan_path = _persist_plan(
        vault_root=vault_root,
        date_for=date_for,
        plan_kind=plan_kind,
        frontmatter=frontmatter,
        body=body,
    )
    return {
        "ok": True,
        "reply": f"Saved plan to fitness/plans/{plan_path.name}.",
        "path": plan_path,
        "plan_id": plan_id,
    }


# ---------------------------------------------------------------------------
# Internal: persist the plan markdown atomically
# ---------------------------------------------------------------------------


def _persist_plan(
    *,
    vault_root: Path,
    date_for: str,
    plan_kind: str,
    frontmatter: str,
    body: str,
) -> Path:
    plans_dir = vault_root / PLANS_RELATIVE
    plans_dir.mkdir(parents=True, exist_ok=True)
    filename = next_plan_filename(date_for=date_for, plan_kind=plan_kind, body=body)
    target = plans_dir / filename
    atomic_write(target, frontmatter + body)
    return target
