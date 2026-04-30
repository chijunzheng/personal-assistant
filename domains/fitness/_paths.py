"""Path constants for the fitness plugin.

Kept in their own module so the helper sub-modules don't need to pull
``handler.py``'s top-level constants in via fragile relative imports.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "FITNESS_RELATIVE",
    "PROFILE_RELATIVE",
    "PROFILE_EVENTS_RELATIVE",
    "WORKOUTS_RELATIVE",
    "MEALS_RELATIVE",
    "METRICS_RELATIVE",
    "MEAL_PHOTOS_RELATIVE",
    "PLANS_RELATIVE",
    "ALIASES_RELATIVE",
    "TEMPLATE_PATH",
]


FITNESS_RELATIVE = Path("fitness")
PROFILE_RELATIVE = FITNESS_RELATIVE / "profile.yaml"
PROFILE_EVENTS_RELATIVE = FITNESS_RELATIVE / "profile_events.jsonl"
WORKOUTS_RELATIVE = FITNESS_RELATIVE / "workouts.jsonl"
MEALS_RELATIVE = FITNESS_RELATIVE / "meals.jsonl"
METRICS_RELATIVE = FITNESS_RELATIVE / "metrics.jsonl"
MEAL_PHOTOS_RELATIVE = FITNESS_RELATIVE / "meal_photos"
PLANS_RELATIVE = FITNESS_RELATIVE / "plans"
ALIASES_RELATIVE = FITNESS_RELATIVE / "_exercise_aliases.yaml"

# Path to the in-plugin profile template (copied on first run to vault).
TEMPLATE_PATH = Path(__file__).parent / "profile.template.yaml"
