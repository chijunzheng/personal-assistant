"""Tests for fitness adaptive plan generation — issue #8.

These tests exercise the load-bearing portfolio demo: workout + nutrition
plan generation via the 7-step in-context recipe (profile + recent
workouts + recent metrics + cross-domain journal grep + last plan ->
``claude_runner`` -> ``vault/fitness/plans/{date}-{kind}-{slug}.md``).

The ``claude_runner`` is mocked everywhere; tests pre-seed the vault with
the inputs each step is supposed to consume, then assert against:

  - the file landing under ``vault/fitness/plans/``,
  - frontmatter ``based_on.recent_workouts`` / ``recent_metrics`` /
    ``journal_cross_refs`` listing only ids/paths actually consulted,
  - body citing real recent context,
  - refusal on a TODO-laced profile,
  - cross-domain reads happening via the filesystem (not by importing
    journal/inventory plugins),
  - idempotency: same inputs -> same plan_id.

Cases ``fit-003``, ``fit-004``, ``fit-005`` from
``domains/fitness/eval/cases.jsonl`` get an explicit harness in this file.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
import yaml

from domains.fitness._query import query_fitness
from domains.fitness.handler import read as fitness_read
from kernel.claude_runner import ClaudeResponse


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fixed_clock() -> datetime:
    """Plan generation runs at 2026-04-29 12:30 UTC."""
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


def _full_profile(**overrides) -> dict:
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


def _seed_profile(vault_root: Path, profile: dict) -> Path:
    fitness = vault_root / "fitness"
    fitness.mkdir(parents=True, exist_ok=True)
    profile_path = fitness / "profile.yaml"
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=True), encoding="utf-8")
    return profile_path


def _seed_workouts(vault_root: Path, rows: list[dict]) -> Path:
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    path = fit / "workouts.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    return path


def _seed_metrics(vault_root: Path, rows: list[dict]) -> Path:
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    path = fit / "metrics.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    return path


def _seed_meals(vault_root: Path, rows: list[dict]) -> Path:
    fit = vault_root / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    path = fit / "meals.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    return path


def _seed_journal(vault_root: Path, entries: dict[str, str]) -> list[Path]:
    """Seed ``vault/journal/<filename>`` with each entry's text. Returns paths."""
    journal = vault_root / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, body in entries.items():
        path = journal / filename
        path.write_text(body, encoding="utf-8")
        written.append(path)
    return written


def _seed_inventory(vault_root: Path, state: dict) -> Path:
    inv = vault_root / "inventory"
    inv.mkdir(parents=True, exist_ok=True)
    path = inv / "state.yaml"
    path.write_text(yaml.safe_dump(state, sort_keys=True), encoding="utf-8")
    return path


def _seed_existing_plan(vault_root: Path, name: str, body: str) -> Path:
    plans = vault_root / "fitness" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / name
    path.write_text(body, encoding="utf-8")
    return path


def _read_plan(path: Path) -> tuple[dict, str]:
    """Split ``---`` frontmatter from the body and return (frontmatter, body)."""
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n"), f"plan missing frontmatter: {path}"
    parts = raw.split("---\n", 2)
    assert len(parts) >= 3, f"malformed plan frontmatter: {path}"
    frontmatter = yaml.safe_load(parts[1]) or {}
    body = parts[2]
    return frontmatter, body


def _stub_invoker(text_factory, calls: Optional[list] = None):
    """Build a fake ``claude_runner.invoke`` that records the prompts it sees."""

    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        if calls is not None:
            calls.append({"prompt": prompt, "system_prompt": system_prompt})
        text = text_factory(prompt) if callable(text_factory) else str(text_factory)
        return ClaudeResponse(
            text=text,
            tokens_in=10,
            tokens_out=20,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def _grounded_plan_text(prompt: str) -> str:
    """Return a deterministic plan body that quotes back ids/paths from the prompt.

    The agent prompt (assembled by the handler) lists the inputs it
    consulted. The deterministic mock 'reads' those listed inputs and
    cites them verbatim — this is how tests assert that the handler
    actually included recent_workouts / journal_cross_refs in the prompt.
    """
    body_lines = [
        "## Why this plan today",
        "Grounded in recent context surfaced by the handler.",
        "",
        "## Session",
        "- Warmup",
    ]
    # Echo any workout ids in the prompt so the body cites real evidence.
    for match in re.findall(r"workout-id:([A-Za-z0-9_-]{2,64})", prompt):
        body_lines.append(f"- references workout {match}")
    for match in re.findall(r"journal-path:([^\s]+)", prompt):
        body_lines.append(f"- journal cross-ref {match}")
    if "sleep" in prompt.lower() and "4.5" in prompt:
        body_lines.append(
            "Recovery is poor; today's session is moderate intensity only."
        )
    if "recomp" in prompt.lower() and "legs" in prompt.lower():
        body_lines.append("Yesterday was legs heavy; today focuses on upper recovery.")
    if "cut" in prompt.lower() and "chicken" in prompt.lower():
        body_lines.append("Targeting 1950 kcal with chicken + rice from inventory.")
    return "\n".join(body_lines) + "\n"


# ---------------------------------------------------------------------------
# 1. refusal: TODO-laced profile
# ---------------------------------------------------------------------------


def test_workout_plan_refuses_when_profile_has_todo(tmp_path: Path) -> None:
    """A profile.yaml with TODO holes must produce a clarifying refusal — no plan written."""
    vault_root = tmp_path / "vault"
    # Profile with explicit TODOs (mimics fresh template copy).
    fitness = vault_root / "fitness"
    fitness.mkdir(parents=True, exist_ok=True)
    (fitness / "profile.yaml").write_text(
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

    reply = fitness_read(
        intent="fitness.workout_plan",
        query="what should I train today?",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_grounded_plan_text),
    )

    assert "profile" in reply.lower()
    assert "todo" in reply.lower() or "fill" in reply.lower()
    plans_dir = vault_root / "fitness" / "plans"
    assert not plans_dir.exists() or not list(plans_dir.glob("*.md"))


# ---------------------------------------------------------------------------
# 2. workout plan: 7-step recipe runs all reads (fit-003 shape)
# ---------------------------------------------------------------------------


def test_workout_plan_writes_markdown_with_grounded_frontmatter(tmp_path: Path) -> None:
    """fit-003 shape: profile + recent legs-heavy workout -> plan cites yesterday's legs."""
    vault_root = tmp_path / "vault"
    _seed_profile(
        vault_root,
        _full_profile(
            goal="recomp",
            weekly_training_days=4,
            equipment_available=["bb", "db", "cable"],
        ),
    )
    workouts = [
        {
            "id": "w-yesterday-legs",
            "date": "2026-04-28",
            "type": "strength",
            "intensity": "hard",
            "tags": ["legs", "heavy"],
            "exercises": [
                {"name": "Back Squat", "sets": 5, "reps": 5, "weight_kg": 120}
            ],
        }
    ]
    _seed_workouts(vault_root, workouts)
    _seed_metrics(vault_root, [])

    calls: list[dict] = []
    reply = fitness_read(
        intent="fitness.workout_plan",
        query="what should I train today?",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_grounded_plan_text, calls=calls),
    )

    assert "saved" in reply.lower() or "plan" in reply.lower()
    plans = list((vault_root / "fitness" / "plans").glob("2026-04-29-workout-*.md"))
    assert len(plans) == 1
    fm, body = _read_plan(plans[0])
    assert fm["kind"] == "workout"
    assert fm["date_for"] == "2026-04-29"
    assert fm["date_generated"].startswith("2026-04-29")
    assert isinstance(fm["plan_id"], str) and len(fm["plan_id"]) == 64
    based = fm["based_on"]
    assert isinstance(based.get("profile_snapshot_sha256"), str)
    assert "w-yesterday-legs" in based.get("recent_workouts", [])
    assert based.get("last_plan_id") is None
    # Mock body cited the workout id (because the prompt listed it).
    assert "w-yesterday-legs" in body
    # The handler must have invoked the LLM exactly once.
    assert len(calls) == 1
    # And the assembled prompt must include all 7 inputs by reference.
    prompt = calls[0]["prompt"]
    assert "profile" in prompt.lower()
    assert "recomp" in prompt.lower()  # goal made it through
    assert "w-yesterday-legs" in prompt  # recent workout id
    assert "legs" in prompt.lower()  # tags propagated


def test_workout_plan_only_lists_real_consulted_workouts(tmp_path: Path) -> None:
    """``based_on.recent_workouts`` includes ONLY ids the handler actually read."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(goal="recomp"))
    workouts = [
        {"id": "w-recent", "date": "2026-04-28", "type": "strength"},
        # Older than 14 days from 2026-04-29 -> must NOT show in recent.
        {"id": "w-stale", "date": "2026-04-01", "type": "strength"},
    ]
    _seed_workouts(vault_root, workouts)
    _seed_metrics(vault_root, [])

    fitness_read(
        intent="fitness.workout_plan",
        query="plan today",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_grounded_plan_text),
    )

    plans = list((vault_root / "fitness" / "plans").glob("2026-04-29-workout-*.md"))
    fm, _ = _read_plan(plans[0])
    recent_ids = fm["based_on"]["recent_workouts"]
    assert "w-recent" in recent_ids
    assert "w-stale" not in recent_ids


# ---------------------------------------------------------------------------
# 3. cross-domain journal grep (fit-004 shape — load-bearing)
# ---------------------------------------------------------------------------


def test_workout_plan_greps_journal_and_downgrades_intensity(tmp_path: Path) -> None:
    """fit-004: poor sleep + journal exhaustion -> moderate plan, no max/PR.

    This is the showcase case for the cross-domain in-context personalization
    thesis. The handler grep's ``vault/journal/`` for the last 14 days, finds
    the exhaustion entry, and threads it into the prompt so the LLM produces
    a recovery-flavored plan.
    """
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(goal="performance", weekly_training_days=5))
    workouts = [
        {"id": "w-2d-ago", "date": "2026-04-27", "type": "strength", "intensity": "moderate"}
    ]
    _seed_workouts(vault_root, workouts)
    metrics = [
        {"id": "m1", "ts": "2026-04-26T07:00:00+00:00", "kind": "sleep_hours", "value": 5.5, "unit": "h"},
        {"id": "m2", "ts": "2026-04-27T07:00:00+00:00", "kind": "sleep_hours", "value": 5.0, "unit": "h"},
        {"id": "m3", "ts": "2026-04-28T07:00:00+00:00", "kind": "sleep_hours", "value": 4.5, "unit": "h"},
    ]
    _seed_metrics(vault_root, metrics)
    journal_paths = _seed_journal(
        vault_root,
        {
            "2026-04-28-exhausted.md": (
                "exhausted today. Three nights of bad sleep, deadline at work. "
                "Body feels heavy."
            ),
            "2026-04-25-random.md": "thoughts on coffee shop",  # no fitness keywords
        },
    )

    fitness_read(
        intent="fitness.workout_plan",
        query="plan my workout today",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_grounded_plan_text),
    )

    plans = list((vault_root / "fitness" / "plans").glob("2026-04-29-workout-*.md"))
    assert len(plans) == 1
    fm, body = _read_plan(plans[0])
    journal_refs = fm["based_on"]["journal_cross_refs"]
    # The exhaustion entry must appear; the unrelated note must NOT.
    matched = [p for p in journal_refs if "exhausted" in p]
    assert len(matched) >= 1, f"expected exhausted entry in {journal_refs}"
    assert all("random" not in p for p in journal_refs)
    # And the body, given sleep=4.5 + journal entry, must downgrade.
    assert "max" not in body.lower()
    assert "pr attempt" not in body.lower()
    body_lower = body.lower()
    assert "moderate" in body_lower or "recovery" in body_lower
    # Recent metric ids in the frontmatter cover the sleep window.
    assert "m3" in fm["based_on"]["recent_metrics"]


def test_workout_plan_grep_does_not_import_journal_handler(tmp_path: Path) -> None:
    """The handler must never import the journal plugin — plugin isolation."""
    import domains.fitness.handler as fh
    src = Path(fh.__file__).read_text(encoding="utf-8")
    assert "from domains.journal" not in src
    assert "import domains.journal" not in src
    # Also verify nothing in domains/fitness/ imports a sibling plugin.
    fit_root = Path(fh.__file__).parent
    for path in fit_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from domains.journal" not in text, path
        assert "import domains.journal" not in text, path
        assert "from domains.inventory" not in text, path
        assert "import domains.inventory" not in text, path


# ---------------------------------------------------------------------------
# 4. nutrition plan: cross-domain inventory + dietary restrictions (fit-005)
# ---------------------------------------------------------------------------


def test_nutrition_plan_consults_inventory_and_respects_restrictions(
    tmp_path: Path,
) -> None:
    """fit-005: cut + lactose-free + chicken/rice/milk inventory -> plan cites chicken+rice, not milk.

    The handler reads ``vault/inventory/state.yaml`` directly (no inventory
    plugin import). The prompt fed to ``claude_runner`` lists what's
    available and what's restricted, so the deterministic mock can
    correctly include chicken/rice and exclude milk.
    """
    vault_root = tmp_path / "vault"
    _seed_profile(
        vault_root,
        _full_profile(
            goal="cut",
            target_calories_kcal=1950,
            target_protein_g=160,
            dietary_restrictions=["lactose-free"],
        ),
    )
    _seed_meals(
        vault_root,
        [
            {
                "id": "meal1",
                "ts": "2026-04-28T08:00:00+00:00",
                "meal_type": "breakfast",
                "total_kcal": 2400,
                "total_protein_g": 110,
                "total_carbs_g": 250,
                "total_fat_g": 80,
            }
        ],
    )
    _seed_metrics(
        vault_root,
        [
            {"id": "wm1", "ts": "2026-04-28T08:00:00+00:00", "kind": "weight", "value": 80.0, "unit": "kg"},
        ],
    )
    _seed_inventory(
        vault_root,
        {
            "chicken breast": {"quantity": 600, "unit": "g"},
            "rice": {"quantity": 1000, "unit": "g"},
            "milk": {"quantity": 1, "unit": "L", "tags": ["dairy"]},
        },
    )

    fitness_read(
        intent="fitness.nutrition_plan",
        query="what should I eat today?",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_grounded_plan_text),
    )

    plans = list((vault_root / "fitness" / "plans").glob("2026-04-29-nutrition-*.md"))
    assert len(plans) == 1, f"expected one nutrition plan, got {plans}"
    fm, body = _read_plan(plans[0])
    assert fm["kind"] == "nutrition"
    body_lower = body.lower()
    assert "chicken" in body_lower
    assert "rice" in body_lower
    assert "1950" in body
    # Restrictions enforced.
    assert "milk" not in body_lower
    assert "yogurt" not in body_lower
    assert "cheese" not in body_lower


# ---------------------------------------------------------------------------
# 5. idempotency: same inputs -> same plan_id
# ---------------------------------------------------------------------------


def test_workout_plan_is_idempotent(tmp_path: Path) -> None:
    """Re-running plan-gen with the same inputs and clock yields the same plan_id."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(goal="recomp"))
    _seed_workouts(
        vault_root,
        [{"id": "w-1", "date": "2026-04-28", "type": "strength", "tags": ["legs"]}],
    )
    _seed_metrics(vault_root, [])

    # Make the mock body identical regardless of micro-changes in prompt
    # (same inputs -> same prompt -> same body anyway, but be defensive).
    def _stable(_p: str) -> str:
        return "## Why this plan today\nidempotent body w-1\n\n## Session\n- Warmup\n"

    fitness_read(
        intent="fitness.workout_plan",
        query="plan today",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_stable),
    )
    fitness_read(
        intent="fitness.workout_plan",
        query="plan today",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_stable),
    )

    plans = sorted((vault_root / "fitness" / "plans").glob("2026-04-29-workout-*.md"))
    assert len(plans) == 1, f"expected idempotent single plan, got {plans}"
    fm, _ = _read_plan(plans[0])
    assert isinstance(fm["plan_id"], str) and len(fm["plan_id"]) == 64


# ---------------------------------------------------------------------------
# 6. last_plan_id linking (progressive overload signal)
# ---------------------------------------------------------------------------


def test_workout_plan_links_previous_plan(tmp_path: Path) -> None:
    """When a workout plan exists in the last 7 days, the new plan links via last_plan_id."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(goal="recomp"))
    _seed_workouts(vault_root, [])
    _seed_metrics(vault_root, [])
    _seed_existing_plan(
        vault_root,
        "2026-04-26-workout-upper.md",
        (
            "---\n"
            "plan_id: " + ("a" * 64) + "\n"
            "kind: workout\n"
            "date_generated: '2026-04-26T10:00:00+00:00'\n"
            "date_for: '2026-04-26'\n"
            "based_on: {}\n"
            "---\n"
            "# Workout — upper\n"
        ),
    )

    fitness_read(
        intent="fitness.workout_plan",
        query="plan today",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_grounded_plan_text),
    )

    plans = sorted((vault_root / "fitness" / "plans").glob("2026-04-29-workout-*.md"))
    assert len(plans) == 1
    fm, _ = _read_plan(plans[0])
    assert fm["based_on"]["last_plan_id"] == "a" * 64


# ---------------------------------------------------------------------------
# 7. compliance — was stubbed in #7, real implementation in #8
# ---------------------------------------------------------------------------


def test_query_fitness_compliance_returns_score_for_existing_plan(tmp_path: Path) -> None:
    """Compliance compares logged workouts to a plan's prescription -> 0..1 score."""
    vault_root = tmp_path / "vault"
    plans_dir = vault_root / "fitness" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_id = "p" * 64
    (plans_dir / "2026-04-28-workout-upper.md").write_text(
        "---\n"
        f"plan_id: {plan_id}\n"
        "kind: workout\n"
        "date_generated: '2026-04-28T10:00:00+00:00'\n"
        "date_for: '2026-04-28'\n"
        "based_on: {}\n"
        "---\n"
        "# Workout — upper\n",
        encoding="utf-8",
    )
    # One workout linked to that plan_id was logged after the prescription.
    fit = vault_root / "fitness"
    (fit / "workouts.jsonl").write_text(
        json.dumps(
            {
                "id": "wlog",
                "date": "2026-04-28",
                "type": "strength",
                "plan_id": plan_id,
                "exercises": [{"name": "Bench Press", "sets": 5, "reps": 5}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = query_fitness(
        kind="compliance",
        plan_id=plan_id,
        compare_to_logs=True,
        vault_root=vault_root,
    )
    assert result["kind"] == "compliance"
    assert result["plan_id"] == plan_id
    assert isinstance(result["value"], (int, float))
    assert 0.0 <= float(result["value"]) <= 1.0
    # With a logged session linked to the plan_id, score must be > 0.
    assert float(result["value"]) > 0.0


def test_query_fitness_compliance_zero_for_unlogged_plan(tmp_path: Path) -> None:
    """A plan with no matching workouts logged -> compliance score 0.0."""
    vault_root = tmp_path / "vault"
    plans_dir = vault_root / "fitness" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_id = "z" * 64
    (plans_dir / "2026-04-28-workout-leg.md").write_text(
        "---\n"
        f"plan_id: {plan_id}\n"
        "kind: workout\n"
        "date_for: '2026-04-28'\n"
        "based_on: {}\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    # No workouts on disk.
    result = query_fitness(
        kind="compliance",
        plan_id=plan_id,
        compare_to_logs=True,
        vault_root=vault_root,
    )
    assert float(result["value"]) == 0.0


# ---------------------------------------------------------------------------
# 8. read rejects unknown plan-style intent
# ---------------------------------------------------------------------------


def test_read_rejects_intent_we_dont_handle(tmp_path: Path) -> None:
    """A junk intent must raise ValueError, not silently produce a plan."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile())
    with pytest.raises(ValueError):
        fitness_read(
            intent="fitness.unknown",
            query="?",
            vault_root=vault_root,
            invoker=_stub_invoker(_grounded_plan_text),
        )


# ---------------------------------------------------------------------------
# 9. orchestrator dispatch: workout_plan / nutrition_plan -> fitness.read
# ---------------------------------------------------------------------------


def _stub_orchestrator_invoker(text_factory):
    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        text = text_factory(prompt) if callable(text_factory) else str(text_factory)
        return ClaudeResponse(
            text=text,
            tokens_in=10,
            tokens_out=20,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def test_orchestrator_dispatches_workout_plan_intent(
    tmp_path: Path, lock_path: Path
) -> None:
    """The orchestrator routes ``fitness.workout_plan`` to fitness.read."""
    from kernel.classifier import Classifier
    from kernel.orchestrator import Orchestrator, SingleInstanceLock

    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    (domains_root / "fitness").mkdir(parents=True, exist_ok=True)
    (domains_root / "fitness" / "domain.yaml").write_text(
        "name: fitness\nintents:\n  - fitness.workout_plan\n", encoding="utf-8"
    )
    _seed_profile(vault_root, _full_profile(goal="recomp"))
    _seed_workouts(vault_root, [{"id": "w-x", "date": "2026-04-28", "type": "strength"}])
    _seed_metrics(vault_root, [])

    classifier = Classifier(
        domains_root=domains_root,
        invoker=_stub_orchestrator_invoker("fitness.workout_plan"),
        prompt_template="",
    )

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_orchestrator_invoker(_grounded_plan_text),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    reply = orchestrator.handle_message("plan today")
    plans = list((vault_root / "fitness" / "plans").glob("2026-04-29-workout-*.md"))
    assert len(plans) == 1
    assert "plan" in reply.text.lower() or "saved" in reply.text.lower()


def test_orchestrator_dispatches_nutrition_plan_intent(
    tmp_path: Path, lock_path: Path
) -> None:
    """The orchestrator routes ``fitness.nutrition_plan`` to fitness.read."""
    from kernel.classifier import Classifier
    from kernel.orchestrator import Orchestrator, SingleInstanceLock

    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    (domains_root / "fitness").mkdir(parents=True, exist_ok=True)
    (domains_root / "fitness" / "domain.yaml").write_text(
        "name: fitness\nintents:\n  - fitness.nutrition_plan\n", encoding="utf-8"
    )
    _seed_profile(
        vault_root,
        _full_profile(
            goal="cut",
            target_calories_kcal=1950,
            target_protein_g=160,
            dietary_restrictions=["lactose-free"],
        ),
    )
    _seed_meals(vault_root, [])
    _seed_metrics(vault_root, [])
    _seed_inventory(
        vault_root,
        {"chicken breast": {"quantity": 500, "unit": "g"}, "rice": {"quantity": 1000, "unit": "g"}},
    )

    classifier = Classifier(
        domains_root=domains_root,
        invoker=_stub_orchestrator_invoker("fitness.nutrition_plan"),
        prompt_template="",
    )
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        invoker=_stub_orchestrator_invoker(_grounded_plan_text),
        classifier=classifier,
        vault_root=vault_root,
        clock=_fixed_clock,
        chat_id="test-chat",
    )

    orchestrator.handle_message("what should I eat?")
    plans = list((vault_root / "fitness" / "plans").glob("2026-04-29-nutrition-*.md"))
    assert len(plans) == 1


# ---------------------------------------------------------------------------
# 10. eval-case harness — fit-003, fit-004, fit-005
# ---------------------------------------------------------------------------


def test_eval_case_fit_003(tmp_path: Path) -> None:
    """fit-003: recomp profile + heavy legs yesterday -> upper-focus plan, frontmatter has based_on."""
    vault_root = tmp_path / "vault"
    _seed_profile(
        vault_root,
        _full_profile(
            goal="recomp",
            weekly_training_days=4,
            equipment_available=["bb", "db", "cable"],
        ),
    )
    _seed_workouts(
        vault_root,
        [
            {
                "id": "fit003-w1",
                "date": "2026-04-28",
                "type": "strength",
                "intensity": "hard",
                "tags": ["legs", "heavy"],
                "exercises": [
                    {"name": "Back Squat", "sets": 5, "reps": 5, "weight_kg": 120}
                ],
            }
        ],
    )
    _seed_metrics(vault_root, [])

    fitness_read(
        intent="fitness.workout_plan",
        query="what should I train today?",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_grounded_plan_text),
    )

    plans = list((vault_root / "fitness" / "plans").glob("2026-04-29-workout-*.md"))
    assert len(plans) == 1
    fm, _ = _read_plan(plans[0])
    assert "plan_id" in fm
    assert "profile_snapshot_sha256" in fm["based_on"]
    assert "fit003-w1" in fm["based_on"]["recent_workouts"]


def test_eval_case_fit_004(tmp_path: Path) -> None:
    """fit-004 (showcase): exhausted journal + 4.5h sleep -> moderate, no max/PR."""
    vault_root = tmp_path / "vault"
    _seed_profile(vault_root, _full_profile(goal="performance", weekly_training_days=5))
    _seed_workouts(
        vault_root,
        [{"id": "fit004-w1", "date": "2026-04-27", "type": "strength", "intensity": "moderate"}],
    )
    _seed_metrics(
        vault_root,
        [
            {"id": "s1", "ts": "2026-04-26T07:00:00+00:00", "kind": "sleep_hours", "value": 5.5, "unit": "h"},
            {"id": "s2", "ts": "2026-04-27T07:00:00+00:00", "kind": "sleep_hours", "value": 5.0, "unit": "h"},
            {"id": "s3", "ts": "2026-04-28T07:00:00+00:00", "kind": "sleep_hours", "value": 4.5, "unit": "h"},
        ],
    )
    _seed_journal(
        vault_root,
        {
            "2026-04-28-fit004.md": (
                "exhausted today. Three nights of bad sleep, deadline at work. "
                "Body feels heavy."
            )
        },
    )

    fitness_read(
        intent="fitness.workout_plan",
        query="plan my workout today",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_grounded_plan_text),
    )

    plans = list((vault_root / "fitness" / "plans").glob("2026-04-29-workout-*.md"))
    assert len(plans) == 1
    fm, body = _read_plan(plans[0])
    journal_refs = fm["based_on"]["journal_cross_refs"]
    assert any("fit004" in p for p in journal_refs)
    body_lower = body.lower()
    assert "max" not in body_lower
    assert "pr attempt" not in body_lower
    assert "moderate" in body_lower or "recovery" in body_lower


def test_eval_case_fit_005(tmp_path: Path) -> None:
    """fit-005: cut + lactose-free + chicken/rice/milk inventory -> respects all signals."""
    vault_root = tmp_path / "vault"
    _seed_profile(
        vault_root,
        _full_profile(
            goal="cut",
            target_calories_kcal=1950,
            target_protein_g=160,
            dietary_restrictions=["lactose-free"],
        ),
    )
    _seed_meals(
        vault_root,
        [
            {
                "id": "fit005-meal1",
                "ts": "2026-04-28T08:00:00+00:00",
                "meal_type": "breakfast",
                "total_kcal": 2400,
                "total_protein_g": 110,
                "total_carbs_g": 250,
                "total_fat_g": 80,
            }
        ],
    )
    _seed_metrics(vault_root, [])
    _seed_inventory(
        vault_root,
        {
            "chicken breast": {"quantity": 600, "unit": "g"},
            "rice": {"quantity": 1000, "unit": "g"},
            "milk": {"quantity": 1, "unit": "L", "tags": ["dairy"]},
        },
    )

    fitness_read(
        intent="fitness.nutrition_plan",
        query="what should I eat today?",
        vault_root=vault_root,
        clock=_fixed_clock,
        invoker=_stub_invoker(_grounded_plan_text),
    )

    plans = list((vault_root / "fitness" / "plans").glob("2026-04-29-nutrition-*.md"))
    assert len(plans) == 1
    _, body = _read_plan(plans[0])
    body_lower = body.lower()
    assert "1950" in body
    assert "chicken" in body_lower
    assert "rice" in body_lower
    assert "milk" not in body_lower
