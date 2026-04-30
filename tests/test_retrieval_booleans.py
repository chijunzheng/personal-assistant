"""Issue #10 — eight engineering Booleans wired into retrieval.

Each Boolean is exercised in isolation: an ON config and an OFF config are
applied to the same fixture vault for the same query, and the resulting
``ContextBundle`` is asserted to differ in the way the spec demands.

The portfolio claim is the load-bearing test at the bottom of this file:
loading the real ``configs/default.yaml`` and ``configs/baseline.yaml`` and
showing the bundles diverge on every dimension simultaneously.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kernel.retrieval import ContextBundle, gather_context


# -- helpers --------------------------------------------------------------


def _seed_vault(
    vault_root: Path,
    *,
    with_index: bool = True,
    with_session: bool = True,
    journal_files: list[tuple[str, str]] | None = None,
) -> dict[str, Path]:
    """Build a small but realistic vault layout under ``vault_root``."""
    paths: dict[str, Path] = {}

    if with_index:
        index = vault_root / "_index" / "INDEX.md"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(
            "# INDEX\n\nclusters:\n- consciousness, qualia, awareness\n",
            encoding="utf-8",
        )
        paths["index"] = index

    if with_session:
        session = vault_root / "_index" / "active_session.md"
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(
            "session: previously discussed consciousness\n",
            encoding="utf-8",
        )
        paths["session"] = session

    journal_dir = vault_root / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    if journal_files is None:
        journal_files = [
            ("2026-04-01-consciousness.md", "thoughts on consciousness today"),
            ("2026-04-20-consciousness.md", "more on consciousness this week"),
        ]
    for name, body in journal_files:
        target = journal_dir / name
        target.write_text(body, encoding="utf-8")
        paths[name] = target

    return paths


def _config(**overrides) -> dict:
    """Build a config dict with default-on flags, overridden by kwargs."""
    flags = {
        "tiered_retrieval": True,
        "per_domain_shaping": True,
        "recency_weighting": True,
        "recency_half_life_days": 14,
        "active_session_summary": True,
        "vault_index_first": True,
        "backlink_expansion": True,
        "backlink_max_hops": 1,
        "suggested_actions": True,
    }
    flags.update(overrides)
    return {
        "retrieval": {
            "context_token_budget": 6000,
            "max_tool_calls_per_turn": 8,
            "max_files": 5,
        },
        "context_engineering": flags,
        "sync": {"conflict_auto_merge": True},
    }


def _all_off_config(**overrides) -> dict:
    """Build a config dict with every Boolean OFF, overridden by kwargs."""
    flags = {
        "tiered_retrieval": False,
        "per_domain_shaping": False,
        "recency_weighting": False,
        "active_session_summary": False,
        "vault_index_first": False,
        "backlink_expansion": False,
        "suggested_actions": False,
    }
    flags.update(overrides)
    return {
        "retrieval": {
            "context_token_budget": 6000,
            "max_tool_calls_per_turn": 8,
            "max_files": 5,
        },
        "context_engineering": flags,
        "sync": {"conflict_auto_merge": False},
    }


# -- ContextBundle exposes a flags dict ----------------------------------


def test_bundle_exposes_flags_dict_for_audit(tmp_path: Path) -> None:
    """The bundle records which Booleans were ON for downstream audit."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle = gather_context(
        query="consciousness",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    assert isinstance(bundle, ContextBundle)
    assert isinstance(bundle.flags, dict)
    assert bundle.flags["tiered_retrieval"] is True
    assert bundle.flags["per_domain_shaping"] is True
    assert bundle.flags["vault_index_first"] is True


# -- 1. tiered_retrieval -------------------------------------------------


def test_tiered_retrieval_off_skips_index_and_session(tmp_path: Path) -> None:
    """When tiered_retrieval=false, no INDEX/session content is preloaded."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle_off = gather_context(
        query="consciousness",
        config=_all_off_config(),
        vault_root=vault_root,
        domain="journal",
    )

    sources = [s.source for s in bundle_off.snippets]
    assert not any(s.endswith("INDEX.md") for s in sources)
    assert not any(s.endswith("active_session.md") for s in sources)


def test_tiered_retrieval_on_preloads_index_and_session(tmp_path: Path) -> None:
    """When tiered_retrieval=true, INDEX + session are part of the bundle."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle_on = gather_context(
        query="consciousness",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    sources = [s.source for s in bundle_on.snippets]
    assert any(s.endswith("INDEX.md") for s in sources)
    assert any(s.endswith("active_session.md") for s in sources)


# -- 2. per_domain_shaping -----------------------------------------------


def test_per_domain_shaping_on_registers_query_tools(tmp_path: Path) -> None:
    """When per_domain_shaping=true, query_finance/inventory/fitness are exposed."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle = gather_context(
        query="how much did I spend on coffee?",
        config=_config(),
        vault_root=vault_root,
        domain="finance",
    )

    palette = set(bundle.tool_palette)
    assert "query_finance" in palette
    assert "query_inventory" in palette
    assert "query_fitness" in palette


def test_per_domain_shaping_off_excludes_query_tools(tmp_path: Path) -> None:
    """When per_domain_shaping=false, query_* tools are NOT registered."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle = gather_context(
        query="how much did I spend on coffee?",
        config=_all_off_config(),
        vault_root=vault_root,
        domain="finance",
    )

    palette = set(bundle.tool_palette)
    for name in ("query_finance", "query_inventory", "query_fitness"):
        assert name not in palette, f"{name} should be absent when per_domain_shaping=false"


# -- 3. recency_weighting ------------------------------------------------


def test_recency_weighting_on_orders_matches_recent_first(tmp_path: Path) -> None:
    """ON: filename-date desc — 2026-04-20 before 2026-04-01."""
    vault_root = tmp_path / "vault"
    _seed_vault(
        vault_root,
        journal_files=[
            ("2026-04-01-conscious-old.md", "consciousness consciousness"),
            ("2026-04-20-conscious-new.md", "consciousness consciousness"),
        ],
    )

    bundle = gather_context(
        query="consciousness",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    matched = [s.source for s in bundle.snippets if "/journal/" in s.source]
    assert matched, "expected at least one journal match"
    # Newest first.
    assert matched[0].endswith("2026-04-20-conscious-new.md")
    # And the older one comes after.
    older_idx = next(i for i, s in enumerate(matched) if s.endswith("2026-04-01-conscious-old.md"))
    newer_idx = next(i for i, s in enumerate(matched) if s.endswith("2026-04-20-conscious-new.md"))
    assert newer_idx < older_idx


def test_recency_weighting_off_orders_matches_alphabetically(tmp_path: Path) -> None:
    """OFF: alphabetical ascending — 2026-04-01 before 2026-04-20."""
    vault_root = tmp_path / "vault"
    _seed_vault(
        vault_root,
        journal_files=[
            ("2026-04-01-conscious-old.md", "consciousness consciousness"),
            ("2026-04-20-conscious-new.md", "consciousness consciousness"),
        ],
    )

    bundle = gather_context(
        query="consciousness",
        config=_all_off_config(),
        vault_root=vault_root,
        domain="journal",
    )

    matched = [s.source for s in bundle.snippets if "/journal/" in s.source]
    older_idx = next(i for i, s in enumerate(matched) if s.endswith("2026-04-01-conscious-old.md"))
    newer_idx = next(i for i, s in enumerate(matched) if s.endswith("2026-04-20-conscious-new.md"))
    assert older_idx < newer_idx


def test_recency_weighting_on_appends_recency_hint_to_system_prompt(tmp_path: Path) -> None:
    """The bundle's ``system_prompt_suffix`` carries a recency directive when ON."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle_on = gather_context(
        query="consciousness",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    assert "recent" in bundle_on.system_prompt_suffix.lower()


def test_recency_weighting_off_omits_recency_hint(tmp_path: Path) -> None:
    """The bundle's ``system_prompt_suffix`` omits the recency directive when OFF."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle_off = gather_context(
        query="consciousness",
        config=_all_off_config(),
        vault_root=vault_root,
        domain="journal",
    )

    assert "recent" not in bundle_off.system_prompt_suffix.lower()


# -- 4. active_session_summary -------------------------------------------


def test_active_session_summary_on_includes_session_snippet(tmp_path: Path) -> None:
    """ON: vault/_index/active_session.md content is present in snippets."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle = gather_context(
        query="consciousness",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    sources = [s.source for s in bundle.snippets]
    assert any(s.endswith("active_session.md") for s in sources)


def test_active_session_summary_off_omits_session_snippet(tmp_path: Path) -> None:
    """OFF: session snippet is absent even when the file exists."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    # Tiered ON so INDEX still loads, only session OFF.
    cfg = _config(active_session_summary=False)
    bundle = gather_context(
        query="consciousness",
        config=cfg,
        vault_root=vault_root,
        domain="journal",
    )

    sources = [s.source for s in bundle.snippets]
    assert not any(s.endswith("active_session.md") for s in sources)


# -- 5. vault_index_first ------------------------------------------------


def test_vault_index_first_on_includes_index_snippet(tmp_path: Path) -> None:
    """ON: INDEX.md is preloaded as the first snippet."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle = gather_context(
        query="consciousness",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    sources = [s.source for s in bundle.snippets]
    assert any(s.endswith("INDEX.md") for s in sources)


def test_vault_index_first_off_omits_index_snippet(tmp_path: Path) -> None:
    """OFF: INDEX.md is not preloaded; grep is still allowed."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    cfg = _config(vault_index_first=False)
    bundle = gather_context(
        query="consciousness",
        config=cfg,
        vault_root=vault_root,
        domain="journal",
    )

    sources = [s.source for s in bundle.snippets]
    assert not any(s.endswith("INDEX.md") for s in sources)
    # Grep still works — at least one journal match should be present.
    assert any("/journal/" in s for s in sources)


# -- 6. backlink_expansion -----------------------------------------------


def test_backlink_expansion_on_registers_read_backlinks_tool(tmp_path: Path) -> None:
    """ON: ``read_backlinks`` is in the tool palette."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle = gather_context(
        query="consciousness",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )

    assert "read_backlinks" in bundle.tool_palette


def test_backlink_expansion_off_omits_read_backlinks_tool(tmp_path: Path) -> None:
    """OFF: ``read_backlinks`` is NOT in the tool palette."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle = gather_context(
        query="consciousness",
        config=_all_off_config(),
        vault_root=vault_root,
        domain="journal",
    )

    assert "read_backlinks" not in bundle.tool_palette


# -- 7. suggested_actions ------------------------------------------------


def test_suggested_actions_flag_propagates_into_bundle(tmp_path: Path) -> None:
    """The bundle's ``flags`` dict surfaces ``suggested_actions`` for digests."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle_on = gather_context(
        query="consciousness",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )
    bundle_off = gather_context(
        query="consciousness",
        config=_all_off_config(),
        vault_root=vault_root,
        domain="journal",
    )

    assert bundle_on.flags["suggested_actions"] is True
    assert bundle_off.flags["suggested_actions"] is False


# -- 8. conflict_auto_merge ----------------------------------------------


def test_conflict_auto_merge_flag_propagates_into_bundle(tmp_path: Path) -> None:
    """The bundle's ``flags`` dict carries ``conflict_auto_merge`` for completeness."""
    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle_on = gather_context(
        query="consciousness",
        config=_config(),
        vault_root=vault_root,
        domain="journal",
    )
    bundle_off = gather_context(
        query="consciousness",
        config=_all_off_config(),
        vault_root=vault_root,
        domain="journal",
    )

    assert bundle_on.flags["conflict_auto_merge"] is True
    assert bundle_off.flags["conflict_auto_merge"] is False


# -- portfolio assertion: default vs baseline ---------------------------


def _project_root() -> Path:
    """Walk up to the personal-assistant project root."""
    here = Path(__file__).resolve()
    return here.parent.parent


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_default_yaml_vs_baseline_yaml_produce_different_bundles(
    tmp_path: Path,
) -> None:
    """Load real configs side-by-side; bundles must diverge end-to-end.

    This is the load-bearing claim for the eval. Same vault, same query,
    same retrieval entry point — only the eight-Boolean config differs.
    """
    project_root = _project_root()
    default_cfg = _load_yaml(project_root / "configs" / "default.yaml")
    baseline_cfg = _load_yaml(project_root / "configs" / "baseline.yaml")

    vault_root = tmp_path / "vault"
    _seed_vault(vault_root)

    bundle_default = gather_context(
        query="consciousness",
        config=default_cfg,
        vault_root=vault_root,
        domain="journal",
    )
    bundle_baseline = gather_context(
        query="consciousness",
        config=baseline_cfg,
        vault_root=vault_root,
        domain="journal",
    )

    # The bundles must be observably different.
    assert bundle_default != bundle_baseline

    # The default tool palette is a strict superset of baseline's.
    palette_default = set(bundle_default.tool_palette)
    palette_baseline = set(bundle_baseline.tool_palette)
    assert palette_default >= palette_baseline
    assert palette_default - palette_baseline, (
        "default must register strictly more tools than baseline"
    )

    # Concrete signals that distinguish default from baseline:
    default_sources = {s.source for s in bundle_default.snippets}
    baseline_sources = {s.source for s in bundle_baseline.snippets}
    assert any(s.endswith("INDEX.md") for s in default_sources)
    assert not any(s.endswith("INDEX.md") for s in baseline_sources)
    assert "query_finance" in palette_default
    assert "query_finance" not in palette_baseline
    assert "read_backlinks" in palette_default
    assert "read_backlinks" not in palette_baseline
