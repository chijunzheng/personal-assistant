"""Tests for ``kernel.views`` — the data-driven view-resolver helper.

The kernel renders a domain's Obsidian markdown view after every write.
``kernel.views`` is the small helper that reads + caches the ``view:``
block from each ``domains/<name>/domain.yaml`` so the orchestrator can
resolve a domain's renderer without ever naming a domain in kernel code
(the cardinal rule — see ADR 2026-05-14).
"""

from __future__ import annotations

from pathlib import Path

from kernel.views import resolve_view_renderer

# The real domains/ tree — plugins are imported as ``domains.<name>`` so the
# resolver's positive path is exercised against the genuine package, the
# same way the orchestrator imports plugins in production.
_REAL_DOMAINS_ROOT = Path(__file__).resolve().parent.parent / "domains"


def _write_domain_yaml(domains_root: Path, name: str, body: str) -> None:
    domain_dir = domains_root / name
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "domain.yaml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Cycle 1 — a domain that declares a view: block resolves to a callable
# ---------------------------------------------------------------------------


def test_resolve_view_renderer_returns_callable_for_declared_view() -> None:
    """The finance domain declares a ``view:`` block -> resolves to its renderer."""
    renderer = resolve_view_renderer("finance", domains_root=_REAL_DOMAINS_ROOT)

    assert renderer is not None
    assert callable(renderer)


# ---------------------------------------------------------------------------
# Cycle 2 — a domain with no view: block resolves to None (degrade silently)
# ---------------------------------------------------------------------------


def test_resolve_view_renderer_returns_none_when_no_view_block(
    tmp_path: Path,
) -> None:
    """A pure-markdown domain that declares no ``view:`` resolves to None."""
    domains_root = tmp_path / "domains"
    _write_domain_yaml(
        domains_root,
        "journal",
        "name: journal\nstorage:\n  type: markdown\n",
    )

    assert resolve_view_renderer("journal", domains_root=domains_root) is None


def test_resolve_view_renderer_returns_none_for_unknown_domain(
    tmp_path: Path,
) -> None:
    """A domain with no domain.yaml at all resolves to None, not an error."""
    domains_root = tmp_path / "domains"
    domains_root.mkdir(parents=True, exist_ok=True)

    assert resolve_view_renderer("ghost", domains_root=domains_root) is None


# ---------------------------------------------------------------------------
# Cycle 3 — resolution is data-driven: kernel never names a domain
# ---------------------------------------------------------------------------


def test_resolve_view_renderer_is_data_driven() -> None:
    """The resolver takes the domain name as data — nothing is hardcoded.

    The same call shape works for ``finance`` (has a view) and any domain
    that does not (returns ``None``). The kernel never branches on a
    literal domain name.
    """
    assert callable(
        resolve_view_renderer("finance", domains_root=_REAL_DOMAINS_ROOT)
    )
    # ``journal`` is pure-markdown — declares no ``view:`` block.
    assert (
        resolve_view_renderer("journal", domains_root=_REAL_DOMAINS_ROOT) is None
    )


def test_resolve_view_renderer_tolerates_malformed_yaml(tmp_path: Path) -> None:
    """A malformed domain.yaml degrades to None rather than crashing the kernel."""
    domains_root = tmp_path / "domains"
    _write_domain_yaml(domains_root, "broken", "name: broken\n  : : bad yaml [")

    assert resolve_view_renderer("broken", domains_root=domains_root) is None


def test_resolve_view_renderer_tolerates_malformed_view_block(
    tmp_path: Path,
) -> None:
    """A ``view:`` block missing the ``module:`` key degrades to None."""
    domains_root = tmp_path / "domains"
    _write_domain_yaml(
        domains_root,
        "halfbaked",
        "name: halfbaked\nstorage:\n  type: jsonl\nview:\n  cadence: weekly\n",
    )

    assert resolve_view_renderer("halfbaked", domains_root=domains_root) is None
