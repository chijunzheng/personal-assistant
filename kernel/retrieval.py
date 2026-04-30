"""Retrieval — config-driven tier order with eight engineering Booleans.

Issue #3 implemented the **minimal** tier order (INDEX -> session -> grep)
with no config knobs. Issue #10 generalizes that into the eight-Boolean
config-driven retrieval contract that lets ``configs/default.yaml`` (all
ON) be measured head-to-head against ``configs/baseline.yaml`` (all OFF).

Each Boolean controls one observable difference in the ``ContextBundle``:

  1. ``tiered_retrieval``        — INDEX + session preload (else: skip)
  2. ``per_domain_shaping``      — register query_finance/inventory/fitness
  3. ``recency_weighting``       — recency-desc grep order + system-prompt hint
  4. ``active_session_summary``  — session preload (sub-tier of #1)
  5. ``vault_index_first``       — INDEX preload (sub-tier of #1)
  6. ``backlink_expansion``      — register read_backlinks(file, max_hops)
  7. ``suggested_actions``       — propagated to digests (no retrieval-side effect)
  8. ``conflict_auto_merge``     — propagated for completeness (no retrieval effect)

The ``ContextBundle`` returned grows four new fields on top of issue #3's
ordered snippets + path list:

  - ``flags``                    — dict of ON/OFF Booleans for this turn
  - ``tool_palette``             — frozenset of tool names registered
  - ``system_prompt_suffix``     — recency directive (when on) for the LLM
  - ``tokens_estimate`` and ``max_tool_calls`` — runtime caps surfaced to the
    orchestrator so audit entries can record bundle weight + tool-call cap

Token budget remains a char-count proxy (4 chars ~ 1 token); ``tokens_estimate``
on the bundle is what the orchestrator audit-logs as
``tokens_in_context_bundle`` so eval can chart bundle weight per turn.

The kernel never changes when adding a new domain plugin — domain query
tools are looked up from the domain handler modules at call time, so
dropping a new ``domains/<name>/handler.py:query_<name>`` is the entire
registration step (consistent with CLAUDE.md's plugin contract).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

__all__ = [
    "ContextBundle",
    "Snippet",
    "expand_keywords",
    "gather_context",
    "read_backlinks",
]


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


_INDEX_RELATIVE = Path("_index") / "INDEX.md"
_SESSION_RELATIVE = Path("_index") / "active_session.md"

# Token budget is a rough char-count proxy in v1: ~4 chars per token is
# the typical ASCII estimate. Precise tokenization can swap in later.
_CHARS_PER_TOKEN = 4

# Sane default file cap so callers that don't configure it still get a
# bounded bundle. The grep tier reads at most this many *matched files*.
_DEFAULT_MAX_FILES = 5

# Matches configs/default.yaml so callers without explicit config still
# get production-shaped headroom.
_DEFAULT_TOKEN_BUDGET = 6000
_DEFAULT_MAX_TOOL_CALLS = 8
_DEFAULT_BACKLINK_MAX_HOPS = 1

# Words shorter than this contribute too much noise as grep terms.
_MIN_TERM_LEN = 3

# Filename-date regex: ``YYYY-MM-DD-...md`` is the convention everywhere
# in the vault, so it serves as a free recency signal when present.
_FILENAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# Wikilink regex: matches ``[[target]]`` and ``[[target|display]]`` forms.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

# The eight context-engineering Booleans, in canonical order. Default to
# True so callers without an explicit ``context_engineering`` block get the
# engineered behavior (preserves issue #3's tests, which pass an empty
# config and expect INDEX + session loads).
_BOOLEAN_FLAGS: tuple[tuple[str, bool], ...] = (
    ("tiered_retrieval", True),
    ("per_domain_shaping", True),
    ("recency_weighting", True),
    ("active_session_summary", True),
    ("vault_index_first", True),
    ("backlink_expansion", True),
    ("suggested_actions", True),
    ("conflict_auto_merge", True),
)

# Tool names — held in module-level frozensets so callers can do membership
# tests without re-creating sets per call.
_FILESYSTEM_TOOLS = frozenset({"read_file", "grep", "list_dir"})
_INDEX_TOOLS = frozenset({"read_index", "read_session"})
_DOMAIN_TOOLS = frozenset({"query_finance", "query_inventory", "query_fitness"})
_EXPANSION_TOOLS = frozenset({"expand_keywords", "read_backlinks"})

# System-prompt suffix appended when ``recency_weighting=true``. Concise
# directive — the agent already has the dated filenames as the primary
# signal; this just nudges the prompt.
_RECENCY_SUFFIX = (
    "When ranking matches, prefer files with the most recent filename date "
    "(YYYY-MM-DD-*.md) or the highest mtime; recent files are typically "
    "the most relevant signal for personal-vault queries."
)


# ---------------------------------------------------------------------------
# public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snippet:
    """One piece of context the agent will see, with provenance."""

    source: str  # absolute path string for stable comparison + display
    text: str


@dataclass(frozen=True)
class ContextBundle:
    """Ordered context the orchestrator hands to the LLM + audit trail.

    Attributes:
        snippets: ordered tier output — INDEX, session, then matched files.
        paths: flat list of every file consulted (for audit + citation).
        flags: which engineering Booleans were ON for this turn (audit).
        tool_palette: frozenset of tool names registered for the agent.
        system_prompt_suffix: extra system-prompt text (e.g., recency hint).
        tokens_estimate: char-proxy estimate of the bundle's token weight.
        max_tool_calls: cap on per-turn tool calls — runtime hint to the agent.
    """

    snippets: tuple[Snippet, ...]
    paths: tuple[Path, ...]
    flags: Mapping[str, bool] = field(default_factory=dict)
    tool_palette: frozenset[str] = field(default_factory=frozenset)
    system_prompt_suffix: str = ""
    tokens_estimate: int = 0
    max_tool_calls: int = _DEFAULT_MAX_TOOL_CALLS


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _read_text_safely(path: Path) -> str | None:
    """Read a file; return ``None`` if missing or unreadable rather than raising."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None


def _retrieval_section(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Pull the ``retrieval`` sub-section from a config map, defaulting to {}."""
    if not config:
        return {}
    return config.get("retrieval") or {}


def _context_engineering_section(
    config: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Pull ``context_engineering`` (or empty) from a config map."""
    if not config:
        return {}
    return config.get("context_engineering") or {}


def _flag(config: Mapping[str, Any] | None, name: str, default: bool) -> bool:
    """Resolve a single Boolean flag from the config with a default fallback."""
    ce = _context_engineering_section(config)
    if name in ce:
        return bool(ce.get(name))
    # Flat-shape fallback for callers that hand us a denormalized dict.
    if isinstance(config, Mapping) and name in config:
        return bool(config.get(name))
    return default


def _resolve_flags(config: Mapping[str, Any] | None) -> dict[str, bool]:
    """Resolve every Boolean flag into a flat dict for the bundle's audit."""
    flags = {name: _flag(config, name, default) for name, default in _BOOLEAN_FLAGS}
    # ``conflict_auto_merge`` lives under sync — let it override the
    # context_engineering default when present so the bundle reflects the
    # actual sync config the conflict_watcher will see.
    sync = (config or {}).get("sync") or {} if isinstance(config, Mapping) else {}
    if isinstance(sync, Mapping) and "conflict_auto_merge" in sync:
        flags["conflict_auto_merge"] = bool(sync.get("conflict_auto_merge"))
    return flags


def _budget_chars(config: Mapping[str, Any] | None) -> int:
    """Return the char-count budget derived from ``retrieval.context_token_budget``."""
    retrieval = _retrieval_section(config)
    budget = int(retrieval.get("context_token_budget", _DEFAULT_TOKEN_BUDGET))
    return max(0, budget) * _CHARS_PER_TOKEN


def _max_tool_calls(config: Mapping[str, Any] | None) -> int:
    """Return the per-turn tool-call cap from ``retrieval.max_tool_calls_per_turn``."""
    retrieval = _retrieval_section(config)
    return max(1, int(retrieval.get("max_tool_calls_per_turn", _DEFAULT_MAX_TOOL_CALLS)))


def _max_files(config: Mapping[str, Any] | None) -> int:
    """Return the cap on matched-file reads from the retrieval config."""
    retrieval = _retrieval_section(config)
    return int(retrieval.get("max_files", _DEFAULT_MAX_FILES))


def _backlink_max_hops(config: Mapping[str, Any] | None) -> int:
    """Return the backlink walk depth cap from the context_engineering config."""
    ce = _context_engineering_section(config)
    return max(0, int(ce.get("backlink_max_hops", _DEFAULT_BACKLINK_MAX_HOPS)))


def _tokenize_query(query: str) -> tuple[str, ...]:
    """Split ``query`` into lowercased word tokens, dropping noise."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", query.lower())
    return tuple(w for w in cleaned.split() if len(w) >= _MIN_TERM_LEN)


def _line_matches_any(line: str, terms: Iterable[str]) -> bool:
    """Cheap substring match — sufficient for v1."""
    lowered = line.lower()
    return any(term in lowered for term in terms)


def _file_matches_query(path: Path, terms: tuple[str, ...]) -> bool:
    """True if any line of the file (or filename) contains any term."""
    if not terms:
        return False
    name_lower = path.name.lower()
    if any(term in name_lower for term in terms):
        return True
    text = _read_text_safely(path)
    if text is None:
        return False
    for line in text.splitlines():
        if _line_matches_any(line, terms):
            return True
    return False


def _filename_date(path: Path) -> str | None:
    """Extract the leading ``YYYY-MM-DD`` from a filename, if any."""
    match = _FILENAME_DATE_RE.match(path.name)
    return match.group(1) if match else None


def _safe_mtime(path: Path) -> float:
    """Return mtime or 0.0 for files that vanished mid-iteration."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _iter_domain_files(
    vault_root: Path,
    domain: str,
    *,
    recency_weighted: bool,
) -> tuple[Path, ...]:
    """Return every ``.md`` under ``<vault>/<domain>/`` in the requested order.

    When ``recency_weighted`` is True: most-recent filename date first
    (with mtime as tie-break for files lacking a date prefix), matching
    the YYYY-MM-DD convention everywhere in the vault. When False:
    plain alphabetical ascending — the unhinted baseline order.
    """
    domain_dir = vault_root / domain
    if not domain_dir.exists():
        return ()
    files = list(domain_dir.glob("*.md"))
    if recency_weighted:
        files.sort(
            key=lambda p: (_filename_date(p) or "", _safe_mtime(p), p.name),
            reverse=True,
        )
    else:
        files.sort(key=lambda p: p.name)
    return tuple(files)


def _push_within_budget(
    snippets: list[Snippet],
    candidate: Snippet,
    *,
    budget_chars: int,
    used_chars: int,
) -> int:
    """Append ``candidate`` if it fits the remaining budget; return new used total.

    Truncates the candidate's text rather than dropping it entirely when
    the remaining budget is positive but smaller than the snippet — keeps
    the provenance link even if the body is partial.
    """
    if budget_chars <= 0:
        snippets.append(candidate)
        return used_chars + len(candidate.text)

    remaining = budget_chars - used_chars
    if remaining <= 0:
        return used_chars

    if len(candidate.text) <= remaining:
        snippets.append(candidate)
        return used_chars + len(candidate.text)

    snippets.append(Snippet(source=candidate.source, text=candidate.text[:remaining]))
    return budget_chars


# ---------------------------------------------------------------------------
# public tools: expand_keywords + read_backlinks
# ---------------------------------------------------------------------------


def expand_keywords(
    seed_query: str,
    *,
    invoker: Optional[Callable[..., Any]] = None,
    max_terms: int = 8,
) -> list[str]:
    """LLM-driven synonym + related-term expansion of a seed query.

    Available regardless of toggle (per kernel/RETRIEVAL.md), but used by
    retrieval ONLY when ``tiered_retrieval`` AND ``vault_index_first`` are
    both ON. The LLM round-trip is via the supplied ``invoker`` (defaults
    to ``kernel.claude_runner.invoke`` so production callers don't have to
    wire it explicitly; tests inject a deterministic stand-in).

    The seed term is always returned first so callers can splice the
    result straight into a grep without re-adding the original.

    Args:
        seed_query: the user's question or topic.
        invoker: pluggable LLM call. Tests pass a stub.
        max_terms: cap on returned terms (seed counts toward the cap).

    Returns:
        Ordered list of terms, seed first, no duplicates, lowercased.
    """
    seed = (seed_query or "").strip()
    if not seed:
        return []

    if invoker is None:
        # Lazy import keeps the module import graph small for tests
        # that monkeypatch around the kernel.
        from kernel.claude_runner import invoke as claude_invoke  # noqa: WPS433

        invoker = claude_invoke

    prompt = (
        "Expand the following query into a comma-separated list of "
        "synonyms and closely related terms suitable for grep over a "
        "personal markdown vault. Return only the comma-separated list, "
        "no preamble.\n\n"
        f"Query: {seed}"
    )

    try:
        response = invoker(prompt)
    except Exception:  # noqa: BLE001
        # Expansion is opportunistic — never fail retrieval if the LLM
        # can't be reached. The seed alone keeps grep working.
        return [seed.lower()]

    raw_text = getattr(response, "text", "") or ""
    candidates = [seed.lower()]
    seen: set[str] = {seed.lower()}
    for chunk in raw_text.replace("\n", ",").split(","):
        term = chunk.strip().lower()
        if not term or term in seen:
            continue
        candidates.append(term)
        seen.add(term)
        if len(candidates) >= max_terms:
            break
    return candidates


def read_backlinks(
    file_path: str | os.PathLike[str],
    *,
    vault_root: str | os.PathLike[str],
    max_hops: int = _DEFAULT_BACKLINK_MAX_HOPS,
) -> list[Path]:
    """Walk ``[[wikilinks]]`` outward from ``file_path`` up to ``max_hops``.

    Returns the *resolved* neighbor paths in BFS order, deduplicated, never
    including the seed file. Resolution is permissive: a wikilink target
    matches any ``.md`` file under ``vault_root`` whose stem equals the
    target (case-insensitive).

    Args:
        file_path: starting file. Missing file -> empty list.
        vault_root: vault root for path resolution.
        max_hops: hop cap (0 = no walk, 1 = direct neighbors only).

    Returns:
        Ordered list of resolved neighbor paths, BFS order, no duplicates.
    """
    seed = Path(file_path)
    root = Path(vault_root)
    if max_hops <= 0 or not seed.exists() or not root.exists():
        return []

    # Pre-build a stem -> path map once so per-link resolution is O(1).
    stem_to_paths: dict[str, list[Path]] = {}
    for candidate in root.rglob("*.md"):
        stem_to_paths.setdefault(candidate.stem.lower(), []).append(candidate)

    visited: set[Path] = {seed.resolve()}
    frontier: list[Path] = [seed]
    out: list[Path] = []

    for _ in range(max_hops):
        next_frontier: list[Path] = []
        for current in frontier:
            text = _read_text_safely(current)
            if text is None:
                continue
            for match in _WIKILINK_RE.finditer(text):
                target = match.group(1).strip()
                if not target:
                    continue
                resolved = stem_to_paths.get(target.lower()) or []
                for hit in resolved:
                    real = hit.resolve()
                    if real in visited:
                        continue
                    visited.add(real)
                    out.append(hit)
                    next_frontier.append(hit)
        frontier = next_frontier
        if not frontier:
            break

    return out


# ---------------------------------------------------------------------------
# tool palette assembly
# ---------------------------------------------------------------------------


def _assemble_tool_palette(flags: Mapping[str, bool]) -> frozenset[str]:
    """Compose the tool palette the agent sees, from the resolved flags.

    The palette is the *registered* surface — what the agent CAN call. The
    *runtime* cap ``max_tool_calls_per_turn`` is conveyed separately on the
    bundle so the agent prompt can say "you may make at most N tool calls".
    Registering more tools than the cap is intentional: the agent picks
    the right N out of M available; truncating the palette would force the
    kernel to guess priorities.
    """
    tools: set[str] = set(_FILESYSTEM_TOOLS)
    # Index tools ride along with tiered_retrieval (since INDEX + session
    # are the entry points of the tier order).
    if flags.get("tiered_retrieval", False):
        tools |= _INDEX_TOOLS
    if flags.get("per_domain_shaping", False):
        tools |= _DOMAIN_TOOLS
    if flags.get("backlink_expansion", False):
        tools.add("read_backlinks")
    # ``expand_keywords`` is exposed when both tiered_retrieval and
    # vault_index_first are on (per kernel/RETRIEVAL.md). It still exists
    # as a callable regardless, but the *tool palette* surface is gated.
    if flags.get("tiered_retrieval", False) and flags.get(
        "vault_index_first", False
    ):
        tools.add("expand_keywords")
    return frozenset(tools)


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def gather_context(
    *,
    query: str,
    config: Mapping[str, Any] | None,
    vault_root: str | os.PathLike[str],
    domain: str,
) -> ContextBundle:
    """Run the configured tier sequence and return what the agent will see.

    Args:
        query: the user's question — drives grep over the domain dir.
        config: full kernel config map. Reads ``retrieval.*`` and
            ``context_engineering.*``; any missing key falls back to a
            default that preserves issue #3's behavior.
        vault_root: vault root on disk. Missing paths degrade gracefully.
        domain: which ``vault/<domain>/`` directory to grep.

    Returns:
        A ``ContextBundle`` with ordered snippets, consulted paths, the
        flags that were ON for this turn, the registered tool palette,
        the system-prompt suffix, and the char-proxy token estimate.
    """
    root = Path(vault_root)
    flags = _resolve_flags(config)
    max_tool_calls = _max_tool_calls(config)
    tool_palette = _assemble_tool_palette(flags)

    suffix = _RECENCY_SUFFIX if flags.get("recency_weighting", False) else ""

    if not root.exists():
        return ContextBundle(
            snippets=(),
            paths=(),
            flags=flags,
            tool_palette=tool_palette,
            system_prompt_suffix=suffix,
            tokens_estimate=0,
            max_tool_calls=max_tool_calls,
        )

    budget = _budget_chars(config)
    max_files = _max_files(config)
    snippets: list[Snippet] = []
    paths: list[Path] = []
    used = 0

    tiered = flags.get("tiered_retrieval", False)
    load_index = tiered and flags.get("vault_index_first", False)
    load_session = tiered and flags.get("active_session_summary", False)
    recency = flags.get("recency_weighting", False)

    # Tier 1 — INDEX.md (if both tiered_retrieval and vault_index_first ON).
    if load_index:
        index_path = root / _INDEX_RELATIVE
        index_text = _read_text_safely(index_path)
        if index_text is not None:
            used = _push_within_budget(
                snippets,
                Snippet(source=str(index_path), text=index_text),
                budget_chars=budget,
                used_chars=used,
            )
            paths.append(index_path)

    # Tier 2 — active_session.md (if both tiered_retrieval and active_session_summary ON).
    if load_session:
        session_path = root / _SESSION_RELATIVE
        session_text = _read_text_safely(session_path)
        if session_text is not None:
            used = _push_within_budget(
                snippets,
                Snippet(source=str(session_path), text=session_text),
                budget_chars=budget,
                used_chars=used,
            )
            paths.append(session_path)

    # Tier 3 + 4 — grep over <domain>/ then read top matches.
    # Recency_weighting controls the order of results; it does NOT gate
    # whether grep runs — grep is the always-on minimum.
    terms = _tokenize_query(query)
    matches: list[Path] = []
    for candidate in _iter_domain_files(root, domain, recency_weighted=recency):
        if len(matches) >= max_files:
            break
        if _file_matches_query(candidate, terms):
            matches.append(candidate)

    for matched in matches:
        body = _read_text_safely(matched)
        if body is None:
            continue
        used = _push_within_budget(
            snippets,
            Snippet(source=str(matched), text=body),
            budget_chars=budget,
            used_chars=used,
        )
        paths.append(matched)

    tokens_estimate = sum(len(s.text) for s in snippets) // _CHARS_PER_TOKEN

    return ContextBundle(
        snippets=tuple(snippets),
        paths=tuple(paths),
        flags=flags,
        tool_palette=tool_palette,
        system_prompt_suffix=suffix,
        tokens_estimate=tokens_estimate,
        max_tool_calls=max_tool_calls,
    )
