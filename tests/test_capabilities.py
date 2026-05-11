"""Tests for ``kernel.capabilities`` -- the user-facing preamble builder."""

from __future__ import annotations

from pathlib import Path

import yaml

from kernel.capabilities import build_capabilities_preamble


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_domain(
    root: Path,
    name: str,
    *,
    description_user_facing: str | None = "Sample domain",
    intent_examples: dict | None = None,
    intents: list | None = None,
    description: str = "internal description",
    extra: dict | None = None,
) -> Path:
    """Create ``domains_root/<name>/domain.yaml`` with the supplied fields.

    Each optional field is included only when non-None so callers can
    exercise the "missing field" cases without manual YAML hacking.
    """
    domain_dir = root / name
    domain_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {"name": name, "description": description}
    if description_user_facing is not None:
        payload["description_user_facing"] = description_user_facing
    if intents is not None:
        payload["intents"] = intents
    if intent_examples is not None:
        payload["intent_examples"] = intent_examples
    if extra:
        payload.update(extra)
    (domain_dir / "domain.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    return domain_dir


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_empty_domains_dir_returns_safe_stub(tmp_path: Path) -> None:
    """An empty domains dir produces a non-empty fallback string.

    The placeholder substitution slot in ``system.md`` always renders
    *something* -- a blank slot would leave the surrounding markdown
    awkwardly empty.
    """
    preamble = build_capabilities_preamble(tmp_path)
    assert preamble.strip() != ""


def test_missing_domains_dir_returns_safe_stub(tmp_path: Path) -> None:
    """A path that doesn't exist degrades to the fallback rather than crashing."""
    preamble = build_capabilities_preamble(tmp_path / "does-not-exist")
    assert preamble.strip() != ""


def test_one_domain_renders_name_description_and_examples(tmp_path: Path) -> None:
    """A populated domain shows up by name with its examples capped at 3."""
    _write_domain(
        tmp_path,
        "fitness",
        description_user_facing="Track workouts and weight.",
        intent_examples={
            "fitness.workout_log": ["did 5x5 squats", "ran a 5k easy"],
            "fitness.query": ["how often did I train last month?"],
        },
    )

    preamble = build_capabilities_preamble(tmp_path)

    assert "Fitness" in preamble
    assert "Track workouts and weight." in preamble
    assert "did 5x5 squats" in preamble
    # Cap at 3: include the third one if present.
    assert "how often did I train last month?" in preamble


def test_examples_cap_at_three_per_domain(tmp_path: Path) -> None:
    """Even with many examples, only the first 3 render to keep the preamble lean."""
    _write_domain(
        tmp_path,
        "fitness",
        description_user_facing="Fitness blurb.",
        intent_examples={
            "i1": ["one", "two"],
            "i2": ["three", "four"],
            "i3": ["five", "six"],
        },
    )

    preamble = build_capabilities_preamble(tmp_path)

    assert "one" in preamble
    assert "two" in preamble
    assert "three" in preamble
    # Past the cap.
    assert "four" not in preamble
    assert "five" not in preamble


def test_missing_description_user_facing_skips_domain(tmp_path: Path) -> None:
    """A domain without the user-facing description is excluded silently.

    Degrade rather than crash: a half-migrated plugin shouldn't take
    down the whole bot's capabilities preamble.
    """
    _write_domain(
        tmp_path,
        "fitness",
        description_user_facing="Fitness description here.",
        intent_examples={"i1": ["foo"]},
    )
    _write_domain(
        tmp_path,
        "legacy",
        description_user_facing=None,  # missing -> skipped
        intent_examples={"i1": ["bar"]},
    )

    preamble = build_capabilities_preamble(tmp_path)

    assert "Fitness" in preamble
    assert "Legacy" not in preamble
    assert "bar" not in preamble  # the legacy example doesn't leak


def test_missing_intent_examples_renders_description_only(tmp_path: Path) -> None:
    """A domain with description but no examples still renders, examples line absent."""
    _write_domain(
        tmp_path,
        "journal",
        description_user_facing="Save thoughts and look them up later.",
        intent_examples=None,
    )

    preamble = build_capabilities_preamble(tmp_path)

    assert "Journal" in preamble
    assert "Save thoughts and look them up later." in preamble
    assert "Examples:" not in preamble


def test_adding_new_domain_yaml_auto_extends_preamble(tmp_path: Path) -> None:
    """The cardinal-rule test: dropping a new domain.yaml extends the preamble.

    No kernel edit, no registration, no import. Just create the file.
    """
    _write_domain(
        tmp_path,
        "fitness",
        description_user_facing="Fitness blurb.",
        intent_examples={"i1": ["lift"]},
    )

    before = build_capabilities_preamble(tmp_path)
    assert "Fitness" in before
    assert "Recipes" not in before

    # Drop a new plugin -- no other change.
    _write_domain(
        tmp_path,
        "recipes",
        description_user_facing="Save recipes and plan meals.",
        intent_examples={"i1": ["save this lasagna recipe"]},
    )

    after = build_capabilities_preamble(tmp_path)
    assert "Fitness" in after
    assert "Recipes" in after
    assert "Save recipes and plan meals." in after
    assert "save this lasagna recipe" in after


def test_underscore_dirs_are_skipped(tmp_path: Path) -> None:
    """Kernel-managed scratch dirs (``_inbox``, ``__pycache__``) are excluded."""
    _write_domain(
        tmp_path,
        "fitness",
        description_user_facing="Fitness blurb.",
        intent_examples={"i1": ["lift"]},
    )
    _write_domain(
        tmp_path,
        "_inbox",
        description_user_facing="Should never appear.",
    )
    _write_domain(
        tmp_path,
        "__pycache__",
        description_user_facing="Should never appear.",
    )

    preamble = build_capabilities_preamble(tmp_path)

    assert "Fitness" in preamble
    assert "Should never appear." not in preamble


def test_directories_without_domain_yaml_are_skipped(tmp_path: Path) -> None:
    """A subdir without ``domain.yaml`` is silently ignored, not raised on."""
    (tmp_path / "scratch").mkdir()
    _write_domain(
        tmp_path,
        "fitness",
        description_user_facing="Fitness blurb.",
        intent_examples={"i1": ["lift"]},
    )

    preamble = build_capabilities_preamble(tmp_path)
    assert "Fitness" in preamble


def test_malformed_yaml_does_not_crash(tmp_path: Path) -> None:
    """A broken YAML file is skipped; healthy domains still render.

    Degrade rather than crash: a typo in one plugin shouldn't take
    down the whole bot.
    """
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "domain.yaml").write_text("not: valid: yaml: at: all:::\n", encoding="utf-8")

    _write_domain(
        tmp_path,
        "fitness",
        description_user_facing="Fitness blurb.",
        intent_examples={"i1": ["lift"]},
    )

    preamble = build_capabilities_preamble(tmp_path)
    assert "Fitness" in preamble


def test_preamble_under_token_budget_for_all_five_real_domains() -> None:
    """Against the real ``domains/`` tree, preamble stays under ~500 tokens.

    Approximated as ~4 chars/token (English). Anchors the ADR's
    "system.md token budget matters" guidance against regression.
    """
    project_root = Path(__file__).resolve().parents[1]
    domains_root = project_root / "domains"
    preamble = build_capabilities_preamble(domains_root)

    # Sanity: the real preamble should at least mention each domain.
    for label in ("Fitness", "Finance", "Journal", "Inventory", "Reminder"):
        assert label in preamble, f"missing {label} from preamble:\n{preamble}"

    # Loose budget check (~4 chars/token).
    approx_tokens = len(preamble) // 4
    assert approx_tokens < 500, (
        f"preamble grew to ~{approx_tokens} tokens; tighten descriptions or "
        f"the per-domain example cap"
    )
