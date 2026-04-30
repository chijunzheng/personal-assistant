"""Profile-state edits + Mifflin-St Jeor macro recomputation.

When the user changes goal / weight / activity_level / target_date, the
target_calories_kcal + macros must recompute so plan generation has fresh
numbers to anchor on. This module owns:

  - the dotted-path resolver / immutable setter for profile.yaml fields,
  - Mifflin-St Jeor + activity-level + goal -> macro target math,
  - the profile-event log writer,
  - the orchestrator-facing ``update_profile_field`` that wires the above
    into one atomic profile.yaml rewrite + N profile_event appends.

Every helper here works against an immutable ``Mapping`` snapshot of the
profile and returns a fresh dict — mutation of the input is forbidden per
the project's coding-style rules.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from kernel.vault import atomic_write

from domains.fitness._io import append_jsonl, existing_ids, load_yaml, sha256_parts
from domains.fitness._paths import PROFILE_EVENTS_RELATIVE, PROFILE_RELATIVE

__all__ = [
    "MACRO_TRIGGER_FIELDS",
    "compute_macro_targets",
    "resolve_field",
    "set_field_immutable",
    "update_profile_field",
]


# Profile fields whose mutation triggers a macro recompute.
MACRO_TRIGGER_FIELDS = ("goal", "weight_kg", "activity_level", "target_date")

# Activity multiplier (Mifflin-St Jeor) by activity_level enum.
_ACTIVITY_MULTIPLIER = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}

# Per-goal kcal adjustment + macro split (protein g/kg, fat g/kg).
_GOAL_PROFILE = {
    "cut":         {"kcal_offset": -500, "protein_g_per_kg": 2.0, "fat_g_per_kg": 0.8},
    "maintain":    {"kcal_offset":    0, "protein_g_per_kg": 1.6, "fat_g_per_kg": 0.9},
    "recomp":      {"kcal_offset": -250, "protein_g_per_kg": 2.0, "fat_g_per_kg": 0.9},
    "bulk":        {"kcal_offset": +400, "protein_g_per_kg": 1.8, "fat_g_per_kg": 1.0},
    "performance": {"kcal_offset":    0, "protein_g_per_kg": 1.8, "fat_g_per_kg": 1.0},
    "rehab":       {"kcal_offset":    0, "protein_g_per_kg": 1.6, "fat_g_per_kg": 0.9},
}


def resolve_field(profile: Mapping[str, Any], dotted: str) -> Any:
    """Walk a dotted-path field name; return ``None`` if any segment misses."""
    cur: Any = profile
    for segment in dotted.split("."):
        if isinstance(cur, Mapping) and segment in cur:
            cur = cur[segment]
        else:
            return None
    return cur


def set_field_immutable(
    profile: Mapping[str, Any],
    dotted: str,
    value: Any,
) -> dict:
    """Return a NEW profile dict with ``dotted`` set to ``value``.

    Immutability discipline: never mutate the input — produce a fresh
    dict / fresh nested dicts so callers can compare before/after safely.
    """
    parts = dotted.split(".")
    new = dict(profile)
    cursor = new
    for segment in parts[:-1]:
        existing = cursor.get(segment)
        nested = dict(existing) if isinstance(existing, Mapping) else {}
        cursor[segment] = nested
        cursor = nested
    cursor[parts[-1]] = value
    return new


def _years_between(start_iso: str, end: datetime) -> float:
    """Float years between an ISO date and ``end`` (tolerates parse failures)."""
    try:
        start = datetime.fromisoformat(str(start_iso))
    except (TypeError, ValueError):
        return 30.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    delta_days = (end - start).days
    return max(0.0, delta_days / 365.25)


def _mifflin_st_jeor(
    *,
    sex: str,
    weight_kg: float,
    height_cm: float,
    age_years: float,
) -> float:
    """Resting metabolic rate per Mifflin-St Jeor."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age_years
    if sex == "m":
        return base + 5
    if sex == "f":
        return base - 161
    # other / null — average the two for a defensible default.
    return base - 78


def compute_macro_targets(
    profile: Mapping[str, Any],
    now: datetime,
) -> dict[str, int]:
    """Compute target_calories_kcal + macro split using Mifflin-St Jeor."""
    sex = str(profile.get("sex") or "other").lower()
    weight_kg = float(profile.get("weight_kg") or 70.0)
    height_cm = float(profile.get("height_cm") or 170.0)
    activity = str(profile.get("activity_level") or "moderate")
    goal = str(profile.get("goal") or "maintain")
    dob = profile.get("date_of_birth") or "1990-01-01"

    age = _years_between(str(dob), now)
    rmr = _mifflin_st_jeor(
        sex=sex,
        weight_kg=weight_kg,
        height_cm=height_cm,
        age_years=age,
    )
    multiplier = _ACTIVITY_MULTIPLIER.get(activity, 1.55)
    tdee = rmr * multiplier

    goal_cfg = _GOAL_PROFILE.get(goal, _GOAL_PROFILE["maintain"])
    kcal = int(round(tdee + goal_cfg["kcal_offset"]))
    protein_g = int(round(weight_kg * goal_cfg["protein_g_per_kg"]))
    fat_g = int(round(weight_kg * goal_cfg["fat_g_per_kg"]))
    # 4 kcal/g protein, 9 kcal/g fat, remainder split to carbs (4 kcal/g).
    remaining = max(0, kcal - protein_g * 4 - fat_g * 9)
    carbs_g = int(round(remaining / 4))

    return {
        "target_calories_kcal": kcal,
        "target_protein_g": protein_g,
        "target_carbs_g": carbs_g,
        "target_fat_g": fat_g,
    }


def _profile_event_id(
    *,
    ts_iso: str,
    field: str,
    old_value: Any,
    new_value: Any,
) -> str:
    return sha256_parts(
        [
            ts_iso,
            field,
            json.dumps(old_value, default=str, sort_keys=True),
            json.dumps(new_value, default=str, sort_keys=True),
        ]
    )


def _append_profile_event(
    *,
    vault_root: Path,
    ts_iso: str,
    field: str,
    old_value: Any,
    new_value: Any,
    source: str,
) -> str:
    """Append one profile_event row; idempotent on id."""
    events_path = vault_root / PROFILE_EVENTS_RELATIVE
    eid = _profile_event_id(
        ts_iso=ts_iso,
        field=field,
        old_value=old_value,
        new_value=new_value,
    )
    seen = existing_ids(events_path)
    if eid in seen:
        return eid
    row = {
        "id": eid,
        "ts": ts_iso,
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "source": source,
    }
    append_jsonl(events_path, row)
    return eid


def _persist_profile(vault_root: Path, profile: Mapping[str, Any]) -> Path:
    """Atomically rewrite ``vault/fitness/profile.yaml`` with ``profile``."""
    target = vault_root / PROFILE_RELATIVE
    atomic_write(target, yaml.safe_dump(dict(profile), sort_keys=True))
    return target


def update_profile_field(
    *,
    field: str,
    new_value: Any,
    vault_root: Path,
    timestamp: datetime,
    source: str,
    recompute_macros: bool,
) -> dict:
    """Rewrite ``profile.yaml:<field>`` + append profile_event(s).

    When ``recompute_macros`` is true and ``field`` is a macro-trigger,
    recompute target_calories_kcal + macros and log those as additional
    profile_event rows.

    Returns a small dict carrying the new profile + the event ids written.
    """
    profile_before = load_yaml(vault_root / PROFILE_RELATIVE)
    old_value = resolve_field(profile_before, field)
    profile_after = set_field_immutable(profile_before, field, new_value)

    macro_event_ids: list[str] = []
    macro_changed_fields: list[str] = []

    if recompute_macros and field in MACRO_TRIGGER_FIELDS:
        macros = compute_macro_targets(profile_after, timestamp)
        for macro_field, macro_value in macros.items():
            previous = resolve_field(profile_after, macro_field)
            if previous == macro_value:
                continue
            profile_after = set_field_immutable(
                profile_after, macro_field, macro_value
            )
            macro_changed_fields.append(macro_field)

    _persist_profile(vault_root, profile_after)

    ts_iso = timestamp.isoformat()
    event_id = _append_profile_event(
        vault_root=vault_root,
        ts_iso=ts_iso,
        field=field,
        old_value=old_value,
        new_value=new_value,
        source=source,
    )

    for macro_field in macro_changed_fields:
        macro_event_ids.append(
            _append_profile_event(
                vault_root=vault_root,
                ts_iso=ts_iso,
                field=macro_field,
                old_value=resolve_field(profile_before, macro_field),
                new_value=resolve_field(profile_after, macro_field),
                source=source,
            )
        )

    return {
        "profile": profile_after,
        "event_id": event_id,
        "macro_event_ids": macro_event_ids,
        "old_value": old_value,
        "new_value": new_value,
    }
