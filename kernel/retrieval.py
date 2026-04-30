"""Retrieval — minimal tier order (issue #3).

The full eight-Boolean wiring (recency weighting, keyword expansion,
backlink walking, per-domain query tools, etc.) lands in issue #10. This
module implements only the **minimal** tier order needed for the journal
query vertical:

    1. Read ``vault/_index/INDEX.md``           (vocabulary seed)
    2. Read ``vault/_index/active_session.md``  (session continuity)
    3. Grep over ``vault/<domain>/`` for any query token
    4. Read up to ``max_files`` matching files into the bundle

The bundle this returns is a small immutable structure: ordered snippets
(what the agent will see) plus a flat list of paths (what the orchestrator
audits and the journal handler cites).

Token budget: v1 uses a rough character-count proxy (chars ~ 4x tokens).
Precise tokenization can be added without changing the public interface.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = ["ContextBundle", "Snippet", "gather_context"]


_INDEX_RELATIVE = Path("_index") / "INDEX.md"
_SESSION_RELATIVE = Path("_index") / "active_session.md"

# Token budget is a rough char-count proxy in v1: ~4 chars per token is
# the typical ASCII estimate. Precise tokenization is issue #10's problem.
_CHARS_PER_TOKEN = 4

# Sane default file cap so callers that don't configure it still get a
# bounded bundle. The minimal tier reads at most this many *matched files*.
_DEFAULT_MAX_FILES = 5

# Pulled from configs/default.yaml to mirror production headroom.
_DEFAULT_TOKEN_BUDGET = 6000

# Words shorter than this contribute too much noise as grep terms (single
# letters, articles, etc.). Matches the spirit of the per-domain tooling
# without pulling in a full stopword list.
_MIN_TERM_LEN = 3


@dataclass(frozen=True)
class Snippet:
    """One piece of context the agent will see, with provenance."""

    source: str  # absolute path string for stable comparison + display
    text: str


@dataclass(frozen=True)
class ContextBundle:
    """Ordered context the orchestrator hands to the LLM + the audit trail.

    Attributes:
        snippets: ordered tier output — INDEX, session, then matched files.
        paths: flat list of every file consulted (for audit + citation).
    """

    snippets: tuple[Snippet, ...]
    paths: tuple[Path, ...]


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


def _budget_chars(config: Mapping[str, Any] | None) -> int:
    """Return the char-count budget derived from ``retrieval.context_token_budget``."""
    retrieval = _retrieval_section(config)
    budget = int(retrieval.get("context_token_budget", _DEFAULT_TOKEN_BUDGET))
    return max(0, budget) * _CHARS_PER_TOKEN


def _max_files(config: Mapping[str, Any] | None) -> int:
    """Return the cap on matched-file reads from the retrieval config."""
    retrieval = _retrieval_section(config)
    return int(retrieval.get("max_files", _DEFAULT_MAX_FILES))


def _tokenize_query(query: str) -> tuple[str, ...]:
    """Split ``query`` into lowercased word tokens, dropping noise."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", query.lower())
    return tuple(w for w in cleaned.split() if len(w) >= _MIN_TERM_LEN)


def _line_matches_any(line: str, terms: Iterable[str]) -> bool:
    """Cheap substring match — sufficient for v1; expansion is issue #10."""
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


def _iter_domain_files(vault_root: Path, domain: str) -> Iterable[Path]:
    """Yield every ``.md`` file under ``<vault>/<domain>/`` in stable order."""
    domain_dir = vault_root / domain
    if not domain_dir.exists():
        return ()
    # Sorted so retrieval is deterministic across runs (filenames carry dates,
    # so "lexicographic descending" doubles as "recency descending" for the
    # ``YYYY-MM-DD-*.md`` convention — issue #10 will weight this explicitly).
    return sorted(domain_dir.glob("*.md"), reverse=True)


def _push_within_budget(
    snippets: list[Snippet],
    candidate: Snippet,
    *,
    budget_chars: int,
    used_chars: int,
) -> int:
    """Append ``candidate`` if it fits the remaining budget; return new used total.

    Truncates the candidate's text rather than dropping it entirely when the
    remaining budget is positive but smaller than the snippet — keeps the
    provenance link even if the body is partial.
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


def gather_context(
    *,
    query: str,
    config: Mapping[str, Any] | None,
    vault_root: str | os.PathLike[str],
    domain: str,
) -> ContextBundle:
    """Run the minimal tier sequence and return what the agent will see.

    Args:
        query: the user's question — drives grep over the domain dir.
        config: retrieval-shaped config map. Reads ``retrieval.context_token_budget``
            and ``retrieval.max_files``; both have safe defaults.
        vault_root: vault root on disk. Missing paths degrade gracefully.
        domain: which ``vault/<domain>/`` directory to grep.

    Returns:
        A ``ContextBundle`` with ordered snippets and a flat path list.
    """
    root = Path(vault_root)
    if not root.exists():
        return ContextBundle(snippets=(), paths=())

    budget = _budget_chars(config)
    max_files = _max_files(config)

    snippets: list[Snippet] = []
    paths: list[Path] = []
    used = 0

    # Tier 1 — INDEX.md
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

    # Tier 2 — active_session.md
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

    # Tier 3 + 4 — grep over <domain>/ then read top matches
    terms = _tokenize_query(query)
    matches: list[Path] = []
    for candidate in _iter_domain_files(root, domain):
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

    return ContextBundle(snippets=tuple(snippets), paths=tuple(paths))
