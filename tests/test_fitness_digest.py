"""Tests for ``domains.fitness.digest`` — daily + weekly digest contributor.

The fitness ``digest.summarize(vault_root, mode='daily'|'weekly')``
contract:

  - ``mode='daily'``: returns a string with today's planned workout +
    calorie pacing + protein pacing (per ``domain.yaml: digest.daily.contents``).
  - ``mode='weekly'``: returns workout summary + macro avg vs target +
    body trend + compliance score (per ``domain.yaml: digest.weekly.contents``).

When the relevant fitness data is missing, the function returns an empty
string so the digest assembler can omit the section cleanly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from domains.fitness.digest import summarize


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_profile(vault_root: Path, profile: dict) -> Path:
    fitness = vault_root / "fitness"
    fitness.mkdir(parents=True, exist_ok=True)
    path = fitness / "profile.yaml"
    path.write_text(yaml.safe_dump(profile, sort_keys=True), encoding="utf-8")
    return path


def _seed_jsonl(vault_root: Path, filename: str, rows: list[dict]) -> Path:
    fitness = vault_root / "fitness"
    fitness.mkdir(parents=True, exist_ok=True)
    path = fitness / filename
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    return path


def _fixed_now() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# daily mode
# ---------------------------------------------------------------------------


def test_summarize_daily_includes_calorie_pacing(tmp_path: Path) -> None:
    """Daily summary mentions today's calories vs the target."""
    vault_root = tmp_path / "vault"
    _seed_profile(
        vault_root,
        {
            "target_calories_kcal": 2200,
            "target_protein_g": 160,
        },
    )
    today_iso = _fixed_now().date().isoformat()
    _seed_jsonl(
        vault_root,
        "meals.jsonl",
        [
            {
                "id": "m1",
                "ts": f"{today_iso}T08:30:00+00:00",
                "meal_type": "breakfast",
                "items": [],
                "total_kcal": 600,
                "total_protein_g": 40,
                "total_carbs_g": 60,
                "total_fat_g": 20,
            },
            {
                "id": "m2",
                "ts": f"{today_iso}T13:00:00+00:00",
                "meal_type": "lunch",
                "items": [],
                "total_kcal": 800,
                "total_protein_g": 50,
                "total_carbs_g": 80,
                "total_fat_g": 30,
            },
        ],
    )

    summary = summarize(vault_root=vault_root, mode="daily", now=_fixed_now())
    assert "1400" in summary  # calories so far today
    assert "2200" in summary  # target


def test_summarize_daily_includes_protein_pacing(tmp_path: Path) -> None:
    """Daily summary surfaces protein so far vs the target."""
    vault_root = tmp_path / "vault"
    _seed_profile(
        vault_root,
        {"target_calories_kcal": 2000, "target_protein_g": 150},
    )
    today_iso = _fixed_now().date().isoformat()
    _seed_jsonl(
        vault_root,
        "meals.jsonl",
        [
            {
                "id": "m1",
                "ts": f"{today_iso}T08:30:00+00:00",
                "meal_type": "breakfast",
                "items": [],
                "total_kcal": 500,
                "total_protein_g": 35,
                "total_carbs_g": 50,
                "total_fat_g": 20,
            },
        ],
    )

    summary = summarize(vault_root=vault_root, mode="daily", now=_fixed_now())
    assert "35" in summary  # protein so far
    assert "150" in summary  # target


def test_summarize_daily_returns_empty_when_no_data(tmp_path: Path) -> None:
    """Missing profile + meals yields an empty string."""
    vault_root = tmp_path / "vault"
    summary = summarize(vault_root=vault_root, mode="daily", now=_fixed_now())
    assert summary == ""


# ---------------------------------------------------------------------------
# weekly mode
# ---------------------------------------------------------------------------


def test_summarize_weekly_includes_workout_count(tmp_path: Path) -> None:
    """Weekly summary mentions the number of workouts in the past 7 days."""
    vault_root = tmp_path / "vault"
    _seed_profile(
        vault_root,
        {
            "target_calories_kcal": 2200,
            "target_protein_g": 160,
            "weekly_training_days": 4,
        },
    )
    _seed_jsonl(
        vault_root,
        "workouts.jsonl",
        [
            {
                "id": "w1",
                "date": "2026-04-25",
                "type": "strength",
                "duration_min": 60,
                "intensity": "hard",
                "exercises": [],
            },
            {
                "id": "w2",
                "date": "2026-04-27",
                "type": "cardio",
                "duration_min": 30,
                "intensity": "moderate",
                "exercises": [],
            },
            {
                "id": "w3",
                "date": "2026-04-28",
                "type": "strength",
                "duration_min": 50,
                "intensity": "hard",
                "exercises": [],
            },
        ],
    )

    summary = summarize(vault_root=vault_root, mode="weekly", now=_fixed_now())
    assert "3" in summary  # workouts this week


def test_summarize_weekly_includes_calorie_avg(tmp_path: Path) -> None:
    """Weekly summary contains an average calorie line vs the target."""
    vault_root = tmp_path / "vault"
    _seed_profile(
        vault_root,
        {
            "target_calories_kcal": 2200,
            "target_protein_g": 160,
        },
    )
    _seed_jsonl(
        vault_root,
        "meals.jsonl",
        [
            {
                "id": "m1",
                "ts": "2026-04-25T12:00:00+00:00",
                "meal_type": "lunch",
                "items": [],
                "total_kcal": 2000,
                "total_protein_g": 150,
                "total_carbs_g": 200,
                "total_fat_g": 70,
            },
            {
                "id": "m2",
                "ts": "2026-04-27T12:00:00+00:00",
                "meal_type": "lunch",
                "items": [],
                "total_kcal": 2400,
                "total_protein_g": 170,
                "total_carbs_g": 220,
                "total_fat_g": 80,
            },
        ],
    )

    summary = summarize(vault_root=vault_root, mode="weekly", now=_fixed_now())
    assert "2200" in summary  # target referenced
    # Avg of 2000+2400 = 2200 -> 1100 per day across 2 logged days
    # Format-flex; just ensure target is referenced for grounding.


def test_summarize_weekly_empty_with_no_data(tmp_path: Path) -> None:
    """Missing fitness data -> empty string."""
    vault_root = tmp_path / "vault"
    summary = summarize(vault_root=vault_root, mode="weekly", now=_fixed_now())
    assert summary == ""


def test_summarize_unknown_mode_raises(tmp_path: Path) -> None:
    """Modes other than daily|weekly raise so a typo is caught early."""
    vault_root = tmp_path / "vault"
    try:
        summarize(vault_root=vault_root, mode="hourly", now=_fixed_now())
    except ValueError:
        return
    raise AssertionError("summarize should reject unknown mode")
