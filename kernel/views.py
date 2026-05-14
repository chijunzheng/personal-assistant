"""Data-driven resolver for domain markdown-view renderers.

ADR 2026-05-14 introduces a kernel primitive: structured-storage domains
declare a ``view:`` block in their ``domain.yaml``; the kernel renders the
view after every write. This module is the small helper that reads that
declaration and hands the orchestrator a callable renderer — without the
kernel ever naming a domain in code.

The cardinal rule applies: resolution is **data-driven**. A new structured
domain registers a view by dropping ``view:`` into its ``domain.yaml`` and
shipping a ``view.py``; nothing here changes. Domains that declare no
``view:`` (pure-markdown domains like ``journal/``) resolve to ``None`` and
the orchestrator's render hook becomes a silent no-op for them.

The ``view:`` block schema::

    view:
      module: view.py:render        # "<file>:<callable>", relative to the domain dir

The resolved renderer's uniform signature (owned by the plugin)::

    def render(vault_root: Path, *, now: datetime) -> None: ...

Resolution degrades rather than crashes: a missing ``domain.yaml``, a
malformed YAML body, a missing ``view:`` block, a ``view:`` block without a
``module:`` key, or an import that fails all return ``None`` so a broken
plugin can never take the kernel down.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Callable, Optional

import yaml

__all__ = ["resolve_view_renderer"]


# The ``module:`` value separates the python file from the callable name
# with a single colon, matching the ``handlers:`` convention already used
# across every ``domain.yaml`` (e.g. ``handler.py:write``).
_MODULE_SPEC_SEP = ":"

# Cache keyed by ``(domains_root, domain)`` — resolution touches the
# filesystem and performs an import, both of which are deterministic for a
# given filesystem state (the capabilities helper makes the same bet).
_RESOLVE_CACHE: dict[tuple[str, str], Optional[Callable[..., None]]] = {}


def _read_view_module_spec(domain_yaml: Path) -> Optional[str]:
    """Return the ``view.module`` string from a ``domain.yaml``, or ``None``.

    Returns ``None`` for any failure mode — missing file, unparseable YAML,
    a non-mapping payload, an absent ``view:`` block, or a ``view:`` block
    that omits the ``module:`` key. The kernel degrades; it does not crash.
    """
    if not domain_yaml.exists():
        return None
    try:
        payload = yaml.safe_load(domain_yaml.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    view = payload.get("view")
    if not isinstance(view, dict):
        return None
    module_spec = view.get("module")
    if not isinstance(module_spec, str) or _MODULE_SPEC_SEP not in module_spec:
        return None
    return module_spec.strip()


def _import_renderer(
    *, domain: str, module_spec: str
) -> Optional[Callable[..., None]]:
    """Lazily import ``<domain>.<file>`` and return its ``<callable>``.

    ``module_spec`` is ``"<file>.py:<callable>"`` relative to the domain
    package, e.g. ``"view.py:render"``. Plugin imports stay lazy (the
    plugin contract) — this function is only ever called from inside the
    orchestrator's per-write hook, never at kernel import time.
    """
    file_part, _, callable_name = module_spec.partition(_MODULE_SPEC_SEP)
    module_stem = file_part[:-3] if file_part.endswith(".py") else file_part
    if not module_stem or not callable_name:
        return None
    dotted = f"domains.{domain}.{module_stem}"
    try:
        module = importlib.import_module(dotted)
    except Exception:  # noqa: BLE001 — a broken plugin must not crash the kernel
        return None
    renderer = getattr(module, callable_name, None)
    return renderer if callable(renderer) else None


def resolve_view_renderer(
    domain: str,
    *,
    domains_root: str | os.PathLike[str],
) -> Optional[Callable[..., None]]:
    """Resolve a domain's registered view renderer from its ``domain.yaml``.

    Args:
        domain: the domain directory name (e.g. ``"finance"``). The kernel
            passes whatever domain it just dispatched a write to — it never
            hardcodes the value.
        domains_root: filesystem path to the ``domains/`` directory.

    Returns:
        The plugin's view renderer callable, or ``None`` when the domain
        declares no usable ``view:`` block. ``None`` is the signal for the
        orchestrator's render hook to no-op silently.
    """
    root = Path(domains_root)
    cache_key = (str(root), domain)
    if cache_key in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[cache_key]

    domain_yaml = root / domain / "domain.yaml"
    module_spec = _read_view_module_spec(domain_yaml)
    renderer = (
        _import_renderer(domain=domain, module_spec=module_spec)
        if module_spec is not None
        else None
    )
    _RESOLVE_CACHE[cache_key] = renderer
    return renderer
