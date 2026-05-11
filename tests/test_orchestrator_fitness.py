"""Integration: orchestrator wires classifier -> fitness handler -> audit -> reply.

Verifies the new ``fitness.*`` dispatch added by issue #7. Plan generation
intents (``fitness.workout_plan``, ``fitness.nutrition_plan``) are issue
#8's responsibility and are NOT exercised here.

  - ``fitness.workout_log | meal_log | metric_log | profile_update`` invoke
    the fitness write path; the corresponding JSONL row appears, an audit
    entry with ``op=write`` + ``domain=fitness`` is appended.
  - Re-sending the same natural-language event does not duplicate rows.
  - ``fitness.query`` invokes the read path and produces a numeric reply.

Both ``claude_runner`` and the LLM-backed extractor are stubbed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from kernel.classifier import Classifier
from kernel.claude_runner import ClaudeResponse
from kernel.orchestrator import Orchestrator, SingleInstanceLock


def _stub_invoker(text: str = "ok"):
    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=1,
            tokens_out=1,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def _seed_fitness_domain(domains_root: Path) -> None:
    domain_dir = domains_root / "fitness"
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "domain.yaml").write_text(
        "name: fitness\n"
        "description: \"workouts and metrics\"\n"
        "intents:\n"
        "  - fitness.workout_log\n"
        "  - fitness.meal_log\n"
        "  - fitness.metric_log\n"
        "  - fitness.profile_update\n"
        "  - fitness.query\n",
        encoding="utf-8",
    )


def _build_classifier(domains_root: Path, intent: str) -> Classifier:
    return Classifier(
        domains_root=domains_root,
        invoker=_stub_invoker(text=intent),
        prompt_template="",
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def _audit_entries(audit_root: Path) -> list[dict]:
    entries: list[dict] = []
    for daily in sorted(audit_root.glob("*.jsonl")):
        for line in daily.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


def _full_profile() -> dict:
    return {
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


def _seed_profile(vault_root: Path, profile: Optional[dict] = None) -> None:
    fitness = vault_root / "fitness"
    fitness.mkdir(parents=True, exist_ok=True)
    (fitness / "profile.yaml").write_text(
        yaml.safe_dump(profile or _full_profile(), sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# fitness.workout_log -> write path
# ---------------------------------------------------------------------------


def test_fitness_workout_log_writes_workouts_jsonl(
    tmp_path: Path, lock_path: Path
) -> None:
    """A fitness.workout_log intent persists a row in workouts.jsonl."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_fitness_domain(domains_root)
    _seed_profile(vault_root)

    classifier = _build_classifier(domains_root, "fitness.workout_log")

    extractor = lambda _msg, _intent: {  # noqa: E731
        "type": "strength",
        "duration_min": 60,
        "intensity": "hard",
        "exercises": [{"name": "Back Squat", "sets": 5, "reps": 5, "weight_kg": 100}],
        "rpe": 8,
        "session_notes": None,
        "tags": [],
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        fitness_extractor=extractor,
    )

    reply = orchestrator.handle_message("did 5x5 squats at 100kg")

    workouts_path = vault_root / "fitness" / "workouts.jsonl"
    assert workouts_path.exists()
    rows = [
        json.loads(line)
        for line in workouts_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["exercises"][0]["name"] == "Back Squat"
    # Reply confirms the operation.
    assert "log" in reply.text.lower() or "saved" in reply.text.lower()


def test_fitness_workout_log_audit_records_write(
    tmp_path: Path, lock_path: Path
) -> None:
    """The orchestrator writes one audit entry with op=write + domain=fitness."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_fitness_domain(domains_root)
    _seed_profile(vault_root)

    classifier = _build_classifier(domains_root, "fitness.workout_log")
    extractor = lambda _m, _i: {  # noqa: E731
        "type": "strength",
        "exercises": [{"name": "Back Squat", "sets": 5, "reps": 5, "weight_kg": 100}],
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        fitness_extractor=extractor,
    )
    orchestrator.handle_message("did 5x5 squats at 100kg")

    write_entries = [
        e for e in _audit_entries(vault_root / "_audit") if e["op"] == "write"
    ]
    assert len(write_entries) == 1
    assert write_entries[0]["domain"] == "fitness"
    assert write_entries[0]["intent"] == "fitness.workout_log"
    assert write_entries[0]["outcome"] == "ok"
    assert "workouts.jsonl" in write_entries[0]["path"]


def test_fitness_workout_log_idempotent(tmp_path: Path, lock_path: Path) -> None:
    """Re-issuing the same workout does not duplicate the JSONL row."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_fitness_domain(domains_root)
    _seed_profile(vault_root)

    classifier = _build_classifier(domains_root, "fitness.workout_log")
    extractor = lambda _m, _i: {  # noqa: E731
        "type": "strength",
        "exercises": [{"name": "Back Squat", "sets": 5, "reps": 5, "weight_kg": 100}],
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        fitness_extractor=extractor,
    )
    orchestrator.handle_message("did 5x5 squats at 100kg")
    orchestrator.handle_message("did 5x5 squats at 100kg")

    workouts_path = vault_root / "fitness" / "workouts.jsonl"
    rows = [
        json.loads(line)
        for line in workouts_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# fitness.meal_log -> write path
# ---------------------------------------------------------------------------


def test_fitness_meal_log_writes_meals_jsonl(
    tmp_path: Path, lock_path: Path
) -> None:
    """A fitness.meal_log intent persists a row in meals.jsonl with totals."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_fitness_domain(domains_root)
    _seed_profile(vault_root)

    classifier = _build_classifier(domains_root, "fitness.meal_log")

    extractor = lambda _m, _i: {  # noqa: E731
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
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        fitness_extractor=extractor,
    )
    orchestrator.handle_message("had 3 eggs and 2 slices wholewheat toast for breakfast")

    meals_path = vault_root / "fitness" / "meals.jsonl"
    rows = [
        json.loads(line)
        for line in meals_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["total_kcal"] == 370
    assert rows[0]["total_protein_g"] == 24


# ---------------------------------------------------------------------------
# fitness.metric_log -> write path (incl. profile.yaml side-effect)
# ---------------------------------------------------------------------------


def test_fitness_metric_log_weight_updates_profile(
    tmp_path: Path, lock_path: Path
) -> None:
    """A weight metric updates profile.yaml AND appends to metrics.jsonl."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_fitness_domain(domains_root)
    _seed_profile(vault_root)

    classifier = _build_classifier(domains_root, "fitness.metric_log")
    extractor = lambda _m, _i: {  # noqa: E731
        "kind": "weight",
        "value": 78.4,
        "unit": "kg",
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        fitness_extractor=extractor,
    )
    orchestrator.handle_message("weighed in at 78.4 this morning")

    metrics_path = vault_root / "fitness" / "metrics.jsonl"
    rows = [
        json.loads(line)
        for line in metrics_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "weight"
    assert rows[0]["value"] == 78.4

    profile = yaml.safe_load(
        (vault_root / "fitness" / "profile.yaml").read_text(encoding="utf-8")
    )
    assert profile["weight_kg"] == 78.4


# ---------------------------------------------------------------------------
# fitness.profile_update -> write path
# ---------------------------------------------------------------------------


def test_fitness_profile_update_rewrites_yaml(
    tmp_path: Path, lock_path: Path
) -> None:
    """A profile_update writes to profile_events.jsonl AND profile.yaml."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_fitness_domain(domains_root)
    _seed_profile(vault_root)

    classifier = _build_classifier(domains_root, "fitness.profile_update")
    extractor = lambda _m, _i: {  # noqa: E731
        "field": "dietary_restrictions",
        "new_value": ["lactose-free"],
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        fitness_extractor=extractor,
    )
    orchestrator.handle_message("add lactose-free to my restrictions")

    profile = yaml.safe_load(
        (vault_root / "fitness" / "profile.yaml").read_text(encoding="utf-8")
    )
    assert profile["dietary_restrictions"] == ["lactose-free"]

    events_path = vault_root / "fitness" / "profile_events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r["field"] == "dietary_restrictions" for r in rows)


# ---------------------------------------------------------------------------
# fitness.query -> read path
# ---------------------------------------------------------------------------


def test_fitness_query_returns_real_count(
    tmp_path: Path, lock_path: Path
) -> None:
    """fitness.query reply includes the real count from query_fitness."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_fitness_domain(domains_root)
    _seed_profile(vault_root)

    # 15 sessions in March.
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": f"w{i}", "date": f"2026-03-{i:02d}", "type": "strength"}
        for i in range(1, 16)
    ]
    (fit / "workouts.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )

    classifier = _build_classifier(domains_root, "fitness.query")

    parser = lambda _q: {  # noqa: E731
        "kind": "workouts",
        "date_range": ("2026-03-01", "2026-03-31"),
        "agg": "count",
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        fitness_query_parser=parser,
    )

    reply = orchestrator.handle_message("how was my training in March?")
    assert "15" in reply.text


def test_fitness_query_audit_records_read(
    tmp_path: Path, lock_path: Path
) -> None:
    """fitness.query produces a read audit entry with domain=fitness."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_fitness_domain(domains_root)
    _seed_profile(vault_root)

    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    (fit / "workouts.jsonl").write_text(
        json.dumps({"id": "w1", "date": "2026-03-15", "type": "strength"}) + "\n",
        encoding="utf-8",
    )

    classifier = _build_classifier(domains_root, "fitness.query")
    parser = lambda _q: {  # noqa: E731
        "kind": "workouts",
        "date_range": ("2026-03-01", "2026-03-31"),
        "agg": "count",
    }

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
        fitness_query_parser=parser,
    )
    orchestrator.handle_message("how was my training in March?")

    read_entries = [
        e for e in _audit_entries(vault_root / "_audit") if e["op"] == "read"
    ]
    assert len(read_entries) == 1
    assert read_entries[0]["domain"] == "fitness"
    assert read_entries[0]["intent"] == "fitness.query"
    assert read_entries[0]["outcome"] == "ok"


# ---------------------------------------------------------------------------
# fitness.workout_plan / fitness.nutrition_plan -> session_update trace (#24)
# ---------------------------------------------------------------------------


def test_fitness_workout_plan_updates_active_session_with_saved_filename(
    tmp_path: Path, lock_path: Path
) -> None:
    """A successful plan write must leave a one-line trace in active_session.md.

    Other handlers (journal, finance, inventory, reminder, fitness-logging)
    all call ``session_update`` after dispatch. The plan handler used to be
    the lone exception, so generated plans were invisible to downstream
    session-aware features (PRD #22 / issue #24). This test pins that the
    trace fires exactly once, includes the plan kind, and includes the
    saved filename.
    """
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    (domains_root / "fitness").mkdir(parents=True, exist_ok=True)
    (domains_root / "fitness" / "domain.yaml").write_text(
        "name: fitness\nintents:\n  - fitness.workout_plan\n", encoding="utf-8"
    )
    _seed_profile(vault_root)
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    (fit / "workouts.jsonl").write_text(
        json.dumps({"id": "w-x", "date": "2026-04-28", "type": "strength"}) + "\n",
        encoding="utf-8",
    )
    (fit / "metrics.jsonl").write_text("", encoding="utf-8")

    classifier = _build_classifier(domains_root, "fitness.workout_plan")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(text="## plan body\n"),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    orchestrator.handle_message("plan today")

    # The plan landed on disk under the expected pattern.
    plans = list((fit / "plans").glob("2026-04-29-workout-*.md"))
    assert len(plans) == 1, f"expected one plan file, got {plans}"
    saved_filename = plans[0].name

    # The session file has a single bullet referencing that filename, with
    # the kind label ("workout") and the relative vault path.
    session_md = vault_root / "_index" / "active_session.md"
    assert session_md.exists()
    contents = session_md.read_text(encoding="utf-8")
    assert saved_filename in contents, (
        f"saved filename {saved_filename!r} missing from session: {contents!r}"
    )
    assert "workout plan saved at fitness/plans/" in contents, (
        f"expected coach-style trace, got: {contents!r}"
    )
    # Exactly one trace line for the plan (turn count == 1 -> one bullet).
    bullets = [
        line for line in contents.splitlines() if line.strip().startswith("- ")
    ]
    plan_bullets = [b for b in bullets if "plan saved at" in b]
    assert len(plan_bullets) == 1, (
        f"expected exactly one plan trace, got {plan_bullets}"
    )


def test_fitness_nutrition_plan_updates_active_session_with_kind_label(
    tmp_path: Path, lock_path: Path
) -> None:
    """The nutrition_plan intent leaves a trace tagged ``nutrition`` (not workout)."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    (domains_root / "fitness").mkdir(parents=True, exist_ok=True)
    (domains_root / "fitness" / "domain.yaml").write_text(
        "name: fitness\nintents:\n  - fitness.nutrition_plan\n", encoding="utf-8"
    )
    profile = _full_profile()
    profile["target_calories_kcal"] = 2000
    profile["target_protein_g"] = 160
    _seed_profile(vault_root, profile)
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    (fit / "meals.jsonl").write_text("", encoding="utf-8")
    (fit / "metrics.jsonl").write_text("", encoding="utf-8")
    inventory = vault_root / "inventory"
    inventory.mkdir(parents=True, exist_ok=True)
    (inventory / "state.yaml").write_text(
        yaml.safe_dump(
            {
                "chicken breast": {"quantity": 500, "unit": "g"},
                "rice": {"quantity": 1000, "unit": "g"},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    classifier = _build_classifier(domains_root, "fitness.nutrition_plan")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(text="## meals\n- breakfast\n"),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    orchestrator.handle_message("what should I eat?")

    plans = list((fit / "plans").glob("2026-04-29-nutrition-*.md"))
    assert len(plans) == 1, f"expected one nutrition plan, got {plans}"

    session_md = vault_root / "_index" / "active_session.md"
    contents = session_md.read_text(encoding="utf-8")
    assert "nutrition plan saved at fitness/plans/" in contents
    assert "workout plan saved at" not in contents


def test_fitness_plan_refusal_does_not_update_active_session(
    tmp_path: Path, lock_path: Path
) -> None:
    """A profile-TODO refusal writes no plan AND leaves no session trace."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    (domains_root / "fitness").mkdir(parents=True, exist_ok=True)
    (domains_root / "fitness" / "domain.yaml").write_text(
        "name: fitness\nintents:\n  - fitness.workout_plan\n", encoding="utf-8"
    )
    # Profile with explicit TODO holes -> handler refuses.
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    (fit / "profile.yaml").write_text(
        "sex: TODO\n"
        "date_of_birth: TODO\n"
        "height_cm: TODO\n"
        "weight_kg: TODO\n"
        "goal: TODO\n"
        "target_weight_kg: TODO\n"
        "target_date: null\n"
        "activity_level: moderate\n"
        "weekly_training_days: 4\n"
        "equipment_available: [bw]\n"
        "dietary_restrictions: []\n"
        "allergies: []\n"
        "injuries_active: []\n"
        "injuries_history: []\n"
        "preferences:\n  enjoys: []\n  avoids: []\n"
        "target_calories_kcal: null\n"
        "target_protein_g: null\n"
        "target_carbs_g: null\n"
        "target_fat_g: null\n"
        "plan_cadence: daily\n",
        encoding="utf-8",
    )

    classifier = _build_classifier(domains_root, "fitness.workout_plan")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(text="## plan body\n"),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    orchestrator.handle_message("plan today")

    # No plan was written.
    plans_dir = fit / "plans"
    assert not plans_dir.exists() or not list(plans_dir.glob("*.md"))

    # The session file may exist (load_or_create materializes it) but must
    # contain NO plan-trace line.
    session_md = vault_root / "_index" / "active_session.md"
    if session_md.exists():
        contents = session_md.read_text(encoding="utf-8")
        assert "plan saved at" not in contents, (
            f"refusal must not leave a plan trace, got: {contents!r}"
        )


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


def test_fitness_unrelated_intents_dont_route_to_fitness(
    tmp_path: Path, lock_path: Path
) -> None:
    """Journal intents must not write to ``vault/fitness/`` — plugin isolation."""
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_fitness_domain(domains_root)

    journal_dir = domains_root / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "domain.yaml").write_text(
        "name: journal\nintents:\n  - journal.capture\n", encoding="utf-8"
    )

    classifier = _build_classifier(domains_root, "journal.capture")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_invoker(),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )
    orchestrator.handle_message("a thought")
    assert not (vault_root / "fitness" / "workouts.jsonl").exists()
    assert not (vault_root / "fitness" / "meals.jsonl").exists()
    assert not (vault_root / "fitness" / "metrics.jsonl").exists()
