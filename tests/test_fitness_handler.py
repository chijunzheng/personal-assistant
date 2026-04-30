"""Tests for ``domains.fitness.handler`` — write + read + query_fitness.

The fitness plugin lives behind a tiny surface but covers the most complex
storage shape in the project: profile.yaml + four event-log JSONLs +
plans/ markdown. This file exercises the write/read/query surface added by
issue #7 (logging only). Plan generation is issue #8's problem.

The ``claude_runner`` is mocked everywhere; tests use a fixed clock and a
temp vault. Specific id values, exercise names, macro numbers, and
file paths are pinned so an implementation drift breaks the test loudly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from domains.fitness.handler import (
    FitnessWriteResult,
    query_fitness,
    read,
    write,
)
from kernel.claude_runner import ClaudeResponse
from kernel.session import Session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_session(chat_id: str = "chat-1", session_id: str = "sess-fit") -> Session:
    return Session(
        chat_id=chat_id,
        session_id=session_id,
        started_at="2026-04-29T10:00:00+00:00",
        last_updated="2026-04-29T10:00:00+00:00",
        turns=0,
        summary="",
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def _stub_extractor(payload: dict):
    """Build a pluggable extractor that returns a fixed parsed payload."""

    def _extract(_message: str, _intent: str) -> dict:
        return dict(payload)

    return _extract


def _stub_invoker(text: str = "ok"):
    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=1,
            tokens_out=1,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _seed_profile(vault_root: Path, profile: dict) -> Path:
    """Drop a profile.yaml directly so tests start populated."""
    fitness = vault_root / "fitness"
    fitness.mkdir(parents=True, exist_ok=True)
    profile_path = fitness / "profile.yaml"
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=True), encoding="utf-8")
    return profile_path


def _full_profile(**overrides) -> dict:
    """Return a no-TODOs profile dict suitable for write tests."""
    base = {
        "sex": "m",
        "date_of_birth": "1995-04-15",
        "height_cm": 178,
        "weight_kg": 80.0,
        "goal": "maintain",
        "target_weight_kg": 80.0,
        "target_date": None,
        "activity_level": "moderate",
        "weekly_training_days": 4,
        "equipment_available": ["bw"],
        "dietary_restrictions": [],
        "allergies": [],
        "injuries_active": [],
        "injuries_history": [],
        "preferences": {"enjoys": [], "avoids": []},
        "target_calories_kcal": None,
        "target_protein_g": None,
        "target_carbs_g": None,
        "target_fat_g": None,
        "plan_cadence": "daily",
    }
    base.update(overrides)
    return base


def _seed_aliases(vault_root: Path, aliases: dict) -> Path:
    """Drop an _exercise_aliases.yaml so workout extraction normalizes through it."""
    fitness = vault_root / "fitness"
    fitness.mkdir(parents=True, exist_ok=True)
    path = fitness / "_exercise_aliases.yaml"
    path.write_text(yaml.safe_dump(aliases, sort_keys=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# first-run profile bootstrap
# ---------------------------------------------------------------------------


def test_write_first_run_copies_profile_template(tmp_path: Path) -> None:
    """A fresh vault gets profile.yaml seeded from the plugin's template."""
    vault_root = tmp_path / "vault"

    write(
        intent="fitness.metric_log",
        message="weighed in at 78.4 this morning",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"kind": "weight", "value": 78.4, "unit": "kg"}
        ),
    )

    profile_path = vault_root / "fitness" / "profile.yaml"
    assert profile_path.exists()
    content = profile_path.read_text(encoding="utf-8")
    # The template's TODO placeholders must be present — handler does NOT fill them.
    assert "TODO" in content
    assert "plan_cadence" in content


def test_write_does_not_clobber_existing_profile(tmp_path: Path) -> None:
    """When profile.yaml already exists, the bootstrap is a no-op."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(weight_kg=82.0))

    write(
        intent="fitness.metric_log",
        message="weighed in at 78.4 this morning",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"kind": "weight", "value": 78.4, "unit": "kg"}
        ),
    )

    profile = _read_yaml(vault_root / "fitness" / "profile.yaml")
    # weight_kg should now be the metric (78.4), but other pre-seeded fields
    # remain — i.e. the file was not blown away by the bootstrap.
    assert profile["sex"] == "m"
    assert profile["height_cm"] == 178
    assert profile["weight_kg"] == 78.4


# ---------------------------------------------------------------------------
# fitness.workout_log
# ---------------------------------------------------------------------------


def test_workout_log_normalizes_exercises_via_aliases(tmp_path: Path) -> None:
    """rdl -> Romanian Deadlift via the optional _exercise_aliases.yaml."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile())
    _seed_aliases(
        vault_root,
        {
            "rdl": "Romanian Deadlift",
            "squat": "Back Squat",
            "bike": "Stationary Bike",
        },
    )

    parsed = {
        "type": "mixed",
        "duration_min": None,
        "intensity": None,
        "exercises": [
            {"name": "squat", "sets": 5, "reps": 5, "weight_kg": 100},
            {"name": "rdl", "sets": 3, "reps": 10, "weight_kg": 80},
            {"name": "bike", "duration_min": 20, "notes": "easy"},
        ],
        "rpe": None,
        "session_notes": None,
        "tags": [],
    }

    result = write(
        intent="fitness.workout_log",
        message="did 5x5 squats at 100kg, 3x10 rdl at 80kg, then 20min easy bike",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(parsed),
    )

    assert isinstance(result, FitnessWriteResult)
    rows = _read_jsonl(vault_root / "fitness" / "workouts.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["exercises"][0]["name"] == "Back Squat"
    assert row["exercises"][1]["name"] == "Romanian Deadlift"
    assert row["exercises"][2]["name"] == "Stationary Bike"
    # id is sha256 hex.
    assert isinstance(row["id"], str) and len(row["id"]) == 64


def test_workout_log_idempotent_on_identical_input(tmp_path: Path) -> None:
    """Re-issuing the same workout extraction does not append a second row."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile())
    _seed_aliases(vault_root, {"squat": "Back Squat"})

    parsed = {
        "type": "strength",
        "duration_min": 60,
        "intensity": "hard",
        "exercises": [{"name": "squat", "sets": 5, "reps": 5, "weight_kg": 100}],
        "rpe": 8,
        "session_notes": None,
        "tags": [],
    }

    first = write(
        intent="fitness.workout_log",
        message="did 5x5 squats at 100kg",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(parsed),
    )
    second = write(
        intent="fitness.workout_log",
        message="did 5x5 squats at 100kg",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(parsed),
    )

    rows = _read_jsonl(vault_root / "fitness" / "workouts.jsonl")
    assert len(rows) == 1
    assert first.appended is True
    assert second.appended is False
    assert first.row_id == second.row_id


def test_workout_log_id_uses_normalized_exercises(tmp_path: Path) -> None:
    """The sha256 id must be deterministic over normalized exercise names."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile())
    _seed_aliases(vault_root, {"rdl": "Romanian Deadlift"})

    parsed = {
        "type": "strength",
        "duration_min": 30,
        "intensity": "moderate",
        "exercises": [{"name": "rdl", "sets": 3, "reps": 10, "weight_kg": 80}],
        "rpe": None,
        "session_notes": None,
        "tags": [],
    }
    result = write(
        intent="fitness.workout_log",
        message="3x10 rdl at 80kg",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(parsed),
    )

    rows = _read_jsonl(vault_root / "fitness" / "workouts.jsonl")
    assert rows[0]["id"] == result.row_id


# ---------------------------------------------------------------------------
# fitness.meal_log
# ---------------------------------------------------------------------------


def test_meal_log_persists_items_with_confidence(tmp_path: Path) -> None:
    """A meal_log row carries items[*].confidence and computed totals."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile())

    parsed = {
        "meal_type": "breakfast",
        "items": [
            {
                "name": "egg",
                "quantity": 3,
                "unit": "count",
                "calories_kcal": 210,
                "protein_g": 18,
                "carbs_g": 0,
                "fat_g": 15,
                "confidence": 0.9,
            },
            {
                "name": "wholewheat toast",
                "quantity": 2,
                "unit": "slice",
                "calories_kcal": 160,
                "protein_g": 6,
                "carbs_g": 30,
                "fat_g": 2,
                "confidence": 0.6,
            },
        ],
        "notes": None,
    }

    result = write(
        intent="fitness.meal_log",
        message="had 3 eggs and 2 slices wholewheat toast for breakfast",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(parsed),
    )

    rows = _read_jsonl(vault_root / "fitness" / "meals.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["meal_type"] == "breakfast"
    assert row["items"][0]["confidence"] == 0.9
    # Totals are summed correctly.
    assert row["total_kcal"] == 370
    assert row["total_protein_g"] == 24
    assert row["total_carbs_g"] == 30
    assert row["total_fat_g"] == 17
    assert isinstance(row["id"], str) and len(row["id"]) == 64
    assert result.appended is True


def test_meal_log_idempotent(tmp_path: Path) -> None:
    """Re-issuing the same meal does not append a second row."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile())

    parsed = {
        "meal_type": "snack",
        "items": [
            {
                "name": "banana",
                "quantity": 1,
                "unit": "count",
                "calories_kcal": 100,
                "protein_g": 1,
                "carbs_g": 27,
                "fat_g": 0,
                "confidence": 0.95,
            }
        ],
        "notes": None,
    }

    write(
        intent="fitness.meal_log",
        message="had a banana",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(parsed),
    )
    write(
        intent="fitness.meal_log",
        message="had a banana",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(parsed),
    )

    rows = _read_jsonl(vault_root / "fitness" / "meals.jsonl")
    assert len(rows) == 1


def test_meal_log_with_photo_stores_under_meal_photos(tmp_path: Path) -> None:
    """When a photo bytes payload is attached, it lands under meal_photos/."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile())

    parsed = {
        "meal_type": "lunch",
        "items": [
            {
                "name": "chicken breast",
                "quantity": 200,
                "unit": "g",
                "calories_kcal": 330,
                "protein_g": 62,
                "carbs_g": 0,
                "fat_g": 7,
                "confidence": 0.8,
            }
        ],
        "notes": None,
    }

    result = write(
        intent="fitness.meal_log",
        message="200g grilled chicken",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(parsed),
        photo_bytes=b"\xff\xd8\xff\xe0fakejpeg",
    )

    photos_dir = vault_root / "fitness" / "meal_photos"
    photos = list(photos_dir.glob("*.jpg"))
    assert len(photos) == 1
    # path is named {date}-{id}.jpg
    assert "2026-04-29" in photos[0].name
    assert result.row_id in photos[0].name

    rows = _read_jsonl(vault_root / "fitness" / "meals.jsonl")
    assert rows[0]["photo_path"] is not None
    assert "meal_photos" in rows[0]["photo_path"]


# ---------------------------------------------------------------------------
# fitness.metric_log
# ---------------------------------------------------------------------------


def test_metric_log_appends_weight_metric(tmp_path: Path) -> None:
    """A weight metric is appended with kind/value/unit and a sha256 id."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(weight_kg=80.0))

    write(
        intent="fitness.metric_log",
        message="weighed in at 78.4 this morning",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"kind": "weight", "value": 78.4, "unit": "kg"}
        ),
    )

    rows = _read_jsonl(vault_root / "fitness" / "metrics.jsonl")
    assert len(rows) == 1
    assert rows[0]["kind"] == "weight"
    assert rows[0]["value"] == 78.4
    assert rows[0]["unit"] == "kg"
    assert isinstance(rows[0]["id"], str) and len(rows[0]["id"]) == 64


def test_metric_log_weight_updates_profile_yaml(tmp_path: Path) -> None:
    """A weight metric also updates profile.yaml:weight_kg."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(weight_kg=80.0))

    write(
        intent="fitness.metric_log",
        message="weighed in at 78.4 this morning",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"kind": "weight", "value": 78.4, "unit": "kg"}
        ),
    )

    profile = _read_yaml(vault_root / "fitness" / "profile.yaml")
    assert profile["weight_kg"] == 78.4


def test_metric_log_weight_logs_profile_event(tmp_path: Path) -> None:
    """A weight metric also drops a row in profile_events.jsonl for the change."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(weight_kg=80.0))

    write(
        intent="fitness.metric_log",
        message="weighed in at 78.4",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"kind": "weight", "value": 78.4, "unit": "kg"}
        ),
    )

    events = _read_jsonl(vault_root / "fitness" / "profile_events.jsonl")
    weight_changes = [e for e in events if e.get("field") == "weight_kg"]
    assert len(weight_changes) == 1
    assert weight_changes[0]["old_value"] == 80.0
    assert weight_changes[0]["new_value"] == 78.4


def test_metric_log_non_weight_kind_does_not_touch_profile(tmp_path: Path) -> None:
    """A sleep metric writes metrics.jsonl but does not edit profile.yaml."""
    vault_root = tmp_path / "vault"
    profile_before = _full_profile(weight_kg=80.0)
    _seed_profile(vault_root, profile_before)

    write(
        intent="fitness.metric_log",
        message="slept 7h last night",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"kind": "sleep_hours", "value": 7.0, "unit": "h"}
        ),
    )

    profile_after = _read_yaml(vault_root / "fitness" / "profile.yaml")
    assert profile_after["weight_kg"] == 80.0
    assert not (vault_root / "fitness" / "profile_events.jsonl").exists() or all(
        e.get("field") != "weight_kg"
        for e in _read_jsonl(vault_root / "fitness" / "profile_events.jsonl")
    )


# ---------------------------------------------------------------------------
# fitness.profile_update
# ---------------------------------------------------------------------------


def test_profile_update_rewrites_yaml_and_logs_event(tmp_path: Path) -> None:
    """Updating dietary_restrictions writes the new value + a profile_event row."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(dietary_restrictions=[]))

    write(
        intent="fitness.profile_update",
        message="add lactose-free to my restrictions",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "field": "dietary_restrictions",
                "new_value": ["lactose-free"],
            }
        ),
    )

    profile = _read_yaml(vault_root / "fitness" / "profile.yaml")
    assert profile["dietary_restrictions"] == ["lactose-free"]

    events = _read_jsonl(vault_root / "fitness" / "profile_events.jsonl")
    diet_changes = [e for e in events if e.get("field") == "dietary_restrictions"]
    assert len(diet_changes) == 1
    assert diet_changes[0]["old_value"] == []
    assert diet_changes[0]["new_value"] == ["lactose-free"]


def test_profile_update_goal_recomputes_macros(tmp_path: Path) -> None:
    """Switching goal=cut recomputes target_calories_kcal + macros."""
    vault_root = tmp_path / "vault"
    _seed_profile(
        vault_root,
        _full_profile(
            sex="m",
            date_of_birth="1995-04-15",
            height_cm=178,
            weight_kg=80.0,
            goal="maintain",
            activity_level="moderate",
            target_calories_kcal=None,
            target_protein_g=None,
        ),
    )

    write(
        intent="fitness.profile_update",
        message="switch me to a cut",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"field": "goal", "new_value": "cut"}
        ),
    )

    profile = _read_yaml(vault_root / "fitness" / "profile.yaml")
    assert profile["goal"] == "cut"
    # Macros must be filled in (non-null integers).
    assert isinstance(profile["target_calories_kcal"], int)
    assert isinstance(profile["target_protein_g"], int)
    # On a cut, calories should be lower than maintenance for a moderate
    # 80kg/178cm/~31y male — Mifflin-StJeor + 1.55 multiplier gives ~2700,
    # cut applies a ~500 kcal deficit, so the result should land in the
    # 2000–2400 range. (Loose bound — implementation defines the exact #.)
    assert 1800 <= profile["target_calories_kcal"] <= 2500
    # Protein floor on a cut: ~1.8–2.4 g/kg, so 80kg => 144–192g.
    assert 140 <= profile["target_protein_g"] <= 200

    # And profile_events.jsonl gets the goal change AND macro recomputes.
    events = _read_jsonl(vault_root / "fitness" / "profile_events.jsonl")
    fields = [e.get("field") for e in events]
    assert "goal" in fields
    assert "target_calories_kcal" in fields
    assert "target_protein_g" in fields


def test_profile_update_non_macro_field_does_not_recompute(tmp_path: Path) -> None:
    """Updating a non-macro field (e.g. enjoys list) does not touch macros."""
    vault_root = tmp_path / "vault"
    _seed_profile(
        vault_root,
        _full_profile(
            target_calories_kcal=2200,
            target_protein_g=160,
            preferences={"enjoys": [], "avoids": []},
        ),
    )

    write(
        intent="fitness.profile_update",
        message="I enjoy lifting",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"field": "preferences.enjoys", "new_value": ["lifting"]}
        ),
    )

    profile = _read_yaml(vault_root / "fitness" / "profile.yaml")
    assert profile["preferences"]["enjoys"] == ["lifting"]
    # Macros unchanged.
    assert profile["target_calories_kcal"] == 2200
    assert profile["target_protein_g"] == 160


# ---------------------------------------------------------------------------
# query_fitness
# ---------------------------------------------------------------------------


def test_query_fitness_workouts_count(tmp_path: Path) -> None:
    """count returns the number of workouts in the date range."""
    vault_root = tmp_path / "vault"
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": "a", "date": "2026-03-02", "type": "strength"},
        {"id": "b", "date": "2026-03-10", "type": "cardio"},
        {"id": "c", "date": "2026-04-15", "type": "strength"},
    ]
    (fit / "workouts.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )

    result = query_fitness(
        kind="workouts",
        date_range=("2026-03-01", "2026-03-31"),
        agg="count",
        vault_root=vault_root,
    )
    assert result["count"] == 2
    assert result["value"] == 2


def test_query_fitness_metrics_trend_for_weight(tmp_path: Path) -> None:
    """trend over weight metrics returns a slope sign + avg."""
    vault_root = tmp_path / "vault"
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": "1", "ts": "2026-04-01T08:00:00+00:00", "kind": "weight", "value": 80.0, "unit": "kg"},
        {"id": "2", "ts": "2026-04-08T08:00:00+00:00", "kind": "weight", "value": 79.5, "unit": "kg"},
        {"id": "3", "ts": "2026-04-15T08:00:00+00:00", "kind": "weight", "value": 79.0, "unit": "kg"},
    ]
    (fit / "metrics.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )

    result = query_fitness(
        kind="metrics",
        metric_kind="weight",
        date_range=("2026-04-01", "2026-04-30"),
        agg="trend",
        vault_root=vault_root,
    )
    assert result["count"] == 3
    assert result["avg"] == 79.5
    # Trend is downward.
    assert result["trend"] < 0


def test_query_fitness_meals_sum_macros(tmp_path: Path) -> None:
    """meals/sum totals macros across rows in the range."""
    vault_root = tmp_path / "vault"
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "id": "m1",
            "ts": "2026-04-29T08:00:00+00:00",
            "meal_type": "breakfast",
            "total_kcal": 400,
            "total_protein_g": 30,
            "total_carbs_g": 40,
            "total_fat_g": 12,
        },
        {
            "id": "m2",
            "ts": "2026-04-29T13:00:00+00:00",
            "meal_type": "lunch",
            "total_kcal": 700,
            "total_protein_g": 50,
            "total_carbs_g": 60,
            "total_fat_g": 22,
        },
    ]
    (fit / "meals.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )

    result = query_fitness(
        kind="meals",
        date_range=("2026-04-29", "2026-04-29"),
        agg="sum",
        vault_root=vault_root,
    )
    assert result["count"] == 2
    assert result["total_kcal"] == 1100
    assert result["total_protein_g"] == 80


def test_query_fitness_profile_returns_state(tmp_path: Path) -> None:
    """kind=profile returns the current profile.yaml content."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(goal="cut", weight_kg=78.0))

    result = query_fitness(kind="profile", vault_root=vault_root)
    assert result["goal"] == "cut"
    assert result["weight_kg"] == 78.0


def test_query_fitness_compliance_stub_returns_na(tmp_path: Path) -> None:
    """kind=compliance is stubbed in #7 (#8 fills it in)."""
    vault_root = tmp_path / "vault"

    result = query_fitness(kind="compliance", vault_root=vault_root)
    assert result.get("status") == "n/a" or result.get("value") == "n/a"


def test_query_fitness_rejects_unknown_kind(tmp_path: Path) -> None:
    """An unknown kind raises ValueError so misroutes are loud."""
    vault_root = tmp_path / "vault"
    try:
        query_fitness(kind="elsewhere", vault_root=vault_root)
    except ValueError:
        return
    raise AssertionError("query_fitness should reject an unknown kind")


# ---------------------------------------------------------------------------
# read — natural-language fitness.query
# ---------------------------------------------------------------------------


def test_read_fitness_query_returns_count_for_workout_query(tmp_path: Path) -> None:
    """How was my training in March -> a real count from query_fitness."""
    vault_root = tmp_path / "vault"
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": f"w{i}", "date": f"2026-03-{i:02d}", "type": "strength"}
        for i in range(1, 16)  # 15 sessions
    ]
    (fit / "workouts.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )

    def _parser(_q: str) -> dict:
        return {
            "kind": "workouts",
            "date_range": ("2026-03-01", "2026-03-31"),
            "agg": "count",
        }

    reply_text = read(
        intent="fitness.query",
        query="how was my training in March?",
        vault_root=vault_root,
        query_parser=_parser,
    )
    assert "15" in reply_text
    # And a hint at the cadence — 15 sessions / 31 days ~ 3.4/wk.
    assert "3." in reply_text


def test_read_fitness_query_rejects_wrong_intent(tmp_path: Path) -> None:
    """Only fitness.query routes through read."""
    vault_root = tmp_path / "vault"
    try:
        read(
            intent="fitness.workout_log",
            query="(unused)",
            vault_root=vault_root,
            query_parser=lambda _q: {"kind": "workouts", "agg": "count"},
        )
    except ValueError:
        return
    raise AssertionError("read should reject a non-query intent")
