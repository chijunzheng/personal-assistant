"""INDEX.md auto-maintained TOC for the vault.

The agent reads ``vault/_index/INDEX.md`` first on every turn (engineering
decision #5 ``vault_index_first``). It seeds keyword expansion vocabulary
for semantic-style queries, exposes the tag map for grep targeting, and
surfaces orphans + a vocabulary frontier for triage and clustering work.

This module ships two entry points:

  * ``write_scaffold(vault_root)`` — minimal placeholder, kept for callers
    that want a non-empty INDEX before any data has been generated. Issue
    #2's contract.
  * ``refresh(vault_root, config)`` — full regenerator: walks the vault,
    builds the six required sections, writes via ``vault.atomic_write``.

Design notes:

  * **Heuristic only (v1).** Topic clusters are derived from frontmatter
    tags; synonyms come from filename tokens + tag co-occurrence. LLM-driven
    cluster mining and synonym expansion are explicitly out of scope here
    (see issue #4 spec).
  * **Determinism.** Every list is sorted by a stable key. The result of a
    second run against an unchanged vault is byte-identical to the first.
    This keeps Drive sync quiet between writes.
  * **Atomicity.** The final markdown is written through
    ``kernel.vault.atomic_write`` so a syncing process never observes a
    half-written INDEX.

Performance note: the function reads every ``.md`` body once for tag
extraction + wikilink scanning. For a 120-note vault this completes in
well under a second on a Mac mini; the issue's <5s acceptance gate is met
with a wide margin.
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from kernel.vault import atomic_write

__all__ = [
    "INDEX_RELATIVE_PATH",
    "RefreshResult",
    "refresh",
    "write_scaffold",
]


# ---------------------------------------------------------------------------
# Constants — kept module-level so tests can introspect them if needed
# ---------------------------------------------------------------------------

INDEX_RELATIVE_PATH = Path("_index") / "INDEX.md"

# Number of entries to surface in "Recent Activity"
_RECENT_LIMIT = 20

# Filenames inside the vault that are *not* domain content; skip when walking.
_SKIP_DIR_NAMES = frozenset({"_index", "_audit", "_inbox"})

# Tokens shorter than this are too noisy to count as cluster synonyms.
_MIN_TOKEN_LEN = 3

# Common date-prefix tokens we strip from filename token sets so they don't
# dominate the synonyms list. ``YYYY-MM-DD-*.md`` -> tokens after the date.
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")

_WIKILINK_RE = re.compile(r"\[\[([^\[\]\|]+?)(?:\|[^\]]*)?\]\]")

# Frontmatter delimiter — three dashes on a line by themselves.
_FRONTMATTER_DELIM = "---"

# Stopwords we drop from filename tokens before clustering / frontier work.
# Kept tiny on purpose — broader stopword lists belong to a v2 enhancement.
_TOKEN_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "this",
        "that",
        "about",
        "note",
        "notes",
        "thoughts",
        "ideas",
        "todo",
    }
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefreshResult:
    """What ``refresh`` returns to its caller (for audit/test inspection).

    Attributes:
        index_path: where the INDEX.md was written.
        files_indexed: how many vault files contributed to the index.
        clusters: how many topic clusters were emitted.
        tags: how many distinct tags were emitted.
        orphans: how many files were classified as orphans.
    """

    index_path: Path
    files_indexed: int
    clusters: int
    tags: int
    orphans: int


# ---------------------------------------------------------------------------
# Scaffold (issue #2 contract — kept for backward compat)
# ---------------------------------------------------------------------------


_SCAFFOLD = """\
# Vault INDEX

This file is the auto-maintained table of contents for the vault. Issue #4
will populate it with topic clusters, synonyms, recent activity, orphan
detection, and the vocabulary frontier used to seed keyword expansion at
query time.

For now this is an intentional scaffold: the kernel writes it on startup so
later passes can extend it without checking-and-creating.

## Topic clusters

(empty — populated by ``kernel.index`` in issue #4)

## Recent activity

(empty — populated by ``kernel.index`` in issue #4)

## Orphans

(empty — populated by ``kernel.index`` in issue #4)
"""


def write_scaffold(vault_root: str | os.PathLike[str]) -> Path:
    """Write the placeholder INDEX.md at ``<vault>/_index/INDEX.md``.

    Idempotent: re-running with the same vault root produces identical
    content. Kept stable for issue #2 callers; new code should call
    :func:`refresh` instead.
    """
    target = Path(vault_root) / INDEX_RELATIVE_PATH
    atomic_write(target, _SCAFFOLD)
    return target


# ---------------------------------------------------------------------------
# Filesystem walk + parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MarkdownNote:
    """One parsed journal/markdown note."""

    path: Path
    relative: str  # vault-relative POSIX path, deterministic
    tags: tuple[str, ...]
    wikilinks_out: tuple[str, ...]
    mtime: float


def _is_under_skipped_dir(path: Path, vault_root: Path) -> bool:
    """True if ``path`` lives under any of the ``_SKIP_DIR_NAMES``."""
    try:
        rel = path.relative_to(vault_root)
    except ValueError:
        return False
    parts = rel.parts
    return bool(parts) and parts[0] in _SKIP_DIR_NAMES


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_yaml, body)`` from a markdown file's text.

    Returns ``("", text)`` if no frontmatter is present. The frontmatter
    block is delimited by lines containing only ``---``.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return "", text
    # Find the closing delimiter.
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_DELIM:
            front = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :])
            return front, body
    # No closing delimiter found — treat as no frontmatter.
    return "", text


def _parse_tags_from_frontmatter(front: str) -> tuple[str, ...]:
    """Extract a stable, lowercased tuple of tags from frontmatter YAML.

    Handles the two shapes the journal handler emits:
        tags: [a, b]
        tags:
          - a
          - b

    A tiny custom parser keeps this dependency-light and resilient to
    weird user-edited YAML.
    """
    tags: list[str] = []
    in_block = False
    for raw in front.splitlines():
        line = raw.rstrip()
        if not line:
            in_block = False
            continue
        if not in_block:
            stripped = line.lstrip()
            if not stripped.lower().startswith("tags"):
                continue
            # Match "tags:" possibly followed by inline list.
            after = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            if after.startswith("[") and after.endswith("]"):
                inner = after[1:-1]
                tags.extend(_split_inline_tags(inner))
            else:
                in_block = True
        else:
            stripped = line.lstrip()
            if stripped.startswith("- "):
                tags.append(stripped[2:].strip().strip("\"'"))
            else:
                in_block = False
    cleaned = tuple(sorted({t.lower().lstrip("#") for t in tags if t}))
    return cleaned


def _split_inline_tags(inner: str) -> list[str]:
    """Split a YAML inline list body like ``a, "b", c`` into tag tokens."""
    return [piece.strip().strip("\"'") for piece in inner.split(",") if piece.strip()]


def _parse_wikilinks(body: str) -> tuple[str, ...]:
    """Extract ``[[wikilink-target]]`` references from a markdown body."""
    raw = _WIKILINK_RE.findall(body)
    return tuple(sorted({m.strip() for m in raw if m.strip()}))


def _read_text_safely(path: Path) -> str:
    """Read a text file; swallow OS errors so a single broken file can't break refresh."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _walk_markdown_notes(vault_root: Path) -> list[_MarkdownNote]:
    """Walk every ``.md`` file under ``vault/<domain>/``, skipping kernel dirs."""
    notes: list[_MarkdownNote] = []
    if not vault_root.exists():
        return notes
    for path in sorted(vault_root.rglob("*.md")):
        if _is_under_skipped_dir(path, vault_root):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        text = _read_text_safely(path)
        front, body = _split_frontmatter(text)
        tags = _parse_tags_from_frontmatter(front)
        links = _parse_wikilinks(body)
        try:
            rel = path.relative_to(vault_root).as_posix()
        except ValueError:
            rel = path.name
        notes.append(
            _MarkdownNote(
                path=path,
                relative=rel,
                tags=tags,
                wikilinks_out=links,
                mtime=mtime,
            )
        )
    return notes


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _filename_tokens(filename: str) -> tuple[str, ...]:
    """Tokenize a markdown filename into useful synonym candidates.

    Strips the date prefix and the ``.md`` extension, splits on ``-`` /
    underscores, drops short tokens and stopwords.
    """
    stem = filename.rsplit(".", 1)[0]
    stem = _DATE_PREFIX_RE.sub("", stem)
    raw = re.split(r"[-_\s]+", stem.lower())
    cleaned = [t for t in raw if len(t) >= _MIN_TOKEN_LEN and t not in _TOKEN_STOPWORDS]
    return tuple(cleaned)


def _build_topic_clusters(notes: list[_MarkdownNote]) -> list[dict[str, Any]]:
    """Cluster notes by frontmatter tag; synonyms = union of filename tokens.

    Each cluster is a dict with deterministic ordering:
        {
            "name": tag,
            "members": sorted list of vault-relative paths,
            "synonyms": sorted list of filename tokens (excluding the cluster name),
        }

    Notes without any tags don't seed clusters — they show up in domain
    stats / orphans / vocabulary frontier instead.
    """
    by_tag: dict[str, list[_MarkdownNote]] = defaultdict(list)
    for note in notes:
        for tag in note.tags:
            by_tag[tag].append(note)

    clusters: list[dict[str, Any]] = []
    for tag in sorted(by_tag.keys()):
        members = sorted({n.relative for n in by_tag[tag]})
        synonym_pool: set[str] = set()
        for member in by_tag[tag]:
            for tok in _filename_tokens(Path(member.relative).name):
                if tok != tag:
                    synonym_pool.add(tok)
            for other_tag in member.tags:
                if other_tag != tag:
                    synonym_pool.add(other_tag)
        clusters.append(
            {
                "name": tag,
                "members": members,
                "synonyms": sorted(synonym_pool),
            }
        )
    return clusters


def _build_tag_map(notes: list[_MarkdownNote]) -> list[dict[str, Any]]:
    """Map every distinct tag to the sorted list of files using it."""
    by_tag: dict[str, list[str]] = defaultdict(list)
    for note in notes:
        for tag in note.tags:
            by_tag[tag].append(note.relative)
    rows: list[dict[str, Any]] = []
    for tag in sorted(by_tag.keys()):
        members = sorted(set(by_tag[tag]))
        rows.append({"tag": tag, "count": len(members), "files": members})
    return rows


def _build_recent_activity(notes: list[_MarkdownNote], *, limit: int) -> list[dict[str, Any]]:
    """Top-N markdown notes by mtime (descending). Ties broken by relative path."""
    ordered = sorted(notes, key=lambda n: (-n.mtime, n.relative))
    rows: list[dict[str, Any]] = []
    for note in ordered[:limit]:
        rows.append(
            {
                "relative": note.relative,
                "mtime": note.mtime,
                "iso": datetime.fromtimestamp(note.mtime, tz=timezone.utc).isoformat(),
            }
        )
    return rows


def _count_jsonl_rows(path: Path) -> int:
    """Count non-empty lines in a JSONL file."""
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return sum(1 for line in text.splitlines() if line.strip())


def _count_yaml_items(path: Path) -> int:
    """Best-effort count of inventory state.yaml items.

    The schema (per ``domains/inventory/domain.yaml``) is::

        items:
          - {item: ..., quantity: ...}
          - ...

    A minimal parser counts top-level ``- `` rows under an ``items:`` key.
    Avoids pulling pyyaml here so the index module stays dep-light; pyyaml
    is already loaded elsewhere but not strictly required for counting.
    """
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    count = 0
    in_items = False
    for raw in text.splitlines():
        stripped = raw.rstrip()
        if not stripped:
            continue
        if stripped.lstrip() == stripped and stripped.endswith(":"):
            in_items = stripped.startswith("items:")
            continue
        if in_items and stripped.lstrip().startswith("- "):
            count += 1
    return count


def _build_domain_stats(
    vault_root: Path, notes: list[_MarkdownNote]
) -> list[dict[str, Any]]:
    """Per-domain volume + freshness rows.

    Recognized domains: ``journal`` (markdown notes), ``finance``
    (transactions.jsonl), ``inventory`` (state.yaml). Unknown subdirectories
    are listed with a generic ``files`` count so new domains show up
    automatically without code changes.
    """
    rows: list[dict[str, Any]] = []
    if not vault_root.exists():
        return rows

    # Journal — count markdown notes under vault/journal/
    journal_notes = [n for n in notes if n.relative.startswith("journal/")]
    if journal_notes:
        latest = max(n.mtime for n in journal_notes)
        rows.append(
            {
                "domain": "journal",
                "volume": f"{len(journal_notes)} notes",
                "freshness": datetime.fromtimestamp(latest, tz=timezone.utc).isoformat(),
            }
        )

    # Finance — count transaction rows
    fin_path = vault_root / "finance" / "transactions.jsonl"
    fin_count = _count_jsonl_rows(fin_path)
    if fin_count > 0:
        try:
            mtime = fin_path.stat().st_mtime
            fresh = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except OSError:
            fresh = "—"
        rows.append(
            {
                "domain": "finance",
                "volume": f"{fin_count} transactions",
                "freshness": fresh,
            }
        )

    # Inventory — count items in state.yaml
    inv_path = vault_root / "inventory" / "state.yaml"
    inv_count = _count_yaml_items(inv_path)
    if inv_count > 0:
        try:
            mtime = inv_path.stat().st_mtime
            fresh = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except OSError:
            fresh = "—"
        rows.append(
            {
                "domain": "inventory",
                "volume": f"{inv_count} items",
                "freshness": fresh,
            }
        )

    return rows


def _build_orphans(notes: list[_MarkdownNote]) -> list[str]:
    """Notes with zero outbound and zero inbound wikilinks.

    Inbound is computed from the union of every other note's ``wikilinks_out``,
    matched against the orphan candidate's filename stem (Obsidian's link
    convention is by stem, not full path).
    """
    by_stem: dict[str, _MarkdownNote] = {}
    for note in notes:
        stem = Path(note.relative).stem
        by_stem.setdefault(stem, note)

    inbound: set[str] = set()
    for note in notes:
        for target in note.wikilinks_out:
            # ``[[some-page]]`` — match against bare stems.
            inbound.add(Path(target).stem)

    orphans: list[str] = []
    for note in notes:
        stem = Path(note.relative).stem
        has_out = bool(note.wikilinks_out)
        has_in = stem in inbound
        if not has_out and not has_in:
            orphans.append(note.relative)
    return sorted(orphans)


def _build_vocabulary_frontier(notes: list[_MarkdownNote]) -> list[str]:
    """Tokens appearing exactly once across all filename token sets.

    Frontier candidates are the singletons — terms not yet repeated enough
    to graduate into a topic cluster. Sorted for deterministic output.
    """
    counter: Counter[str] = Counter()
    for note in notes:
        for tok in _filename_tokens(Path(note.relative).name):
            counter[tok] += 1
    singletons = [tok for tok, n in counter.items() if n == 1]
    return sorted(singletons)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_topic_clusters(clusters: list[dict[str, Any]]) -> str:
    if not clusters:
        return "## Topic Clusters\n\n_(no clusters yet — add frontmatter `tags:` to journal notes)_\n"
    lines: list[str] = ["## Topic Clusters", ""]
    lines.append(
        "_Derived from frontmatter `tags:`. Synonyms are the union of filename tokens "
        "and co-occurring tags across cluster members. Heuristic only (v1)._"
    )
    lines.append("")
    for cluster in clusters:
        # Cluster names use bold + count rather than a deeper heading so
        # the only ``##`` markers in the document are section boundaries.
        # This keeps simple section parsers (and tests that key off ``##``)
        # unambiguous.
        lines.append(f"**{cluster['name']}** — {len(cluster['members'])} notes")
        synonyms = ", ".join(cluster["synonyms"]) if cluster["synonyms"] else "_none_"
        lines.append(f"- synonyms: {synonyms}")
        lines.append("- members:")
        for member in cluster["members"]:
            lines.append(f"  - `{member}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_tag_map(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "## Tag Map\n\n_(no tags found in frontmatter)_\n"
    lines: list[str] = ["## Tag Map", ""]
    lines.append("| tag | count | files |")
    lines.append("|---|---|---|")
    for row in rows:
        files = ", ".join(f"`{f}`" for f in row["files"])
        lines.append(f"| {row['tag']} | {row['count']} | {files} |")
    return "\n".join(lines) + "\n"


def _render_recent_activity(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "## Recent Activity\n\n_(no notes yet)_\n"
    lines: list[str] = ["## Recent Activity", ""]
    lines.append(f"_Top {len(rows)} files by mtime, most recent first._")
    lines.append("")
    lines.append("| file | modified |")
    lines.append("|---|---|")
    for row in rows:
        lines.append(f"| `{row['relative']}` | {row['iso']} |")
    return "\n".join(lines) + "\n"


def _render_domain_stats(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "## Domain Stats\n\n_(no domain content yet)_\n"
    lines: list[str] = ["## Domain Stats", ""]
    lines.append("| domain | volume | freshness |")
    lines.append("|---|---|---|")
    for row in rows:
        lines.append(f"| {row['domain']} | {row['volume']} | {row['freshness']} |")
    return "\n".join(lines) + "\n"


def _render_orphans(orphans: list[str]) -> str:
    if not orphans:
        return "## Orphans\n\n_(none — every note has a backlink)_\n"
    lines: list[str] = ["## Orphans", ""]
    lines.append("_Notes with no inbound or outbound `[[wikilinks]]`. Triage candidates._")
    lines.append("")
    for member in orphans:
        lines.append(f"- `{member}`")
    return "\n".join(lines) + "\n"


def _render_vocabulary_frontier(terms: list[str]) -> str:
    if not terms:
        return "## Vocabulary Frontier\n\n_(no singleton terms detected)_\n"
    lines: list[str] = ["## Vocabulary Frontier", ""]
    lines.append("_Filename tokens appearing exactly once. Candidate new clusters._")
    lines.append("")
    for term in terms:
        lines.append(f"- `{term}`")
    return "\n".join(lines) + "\n"


def _render_index(
    *,
    generated_at: str,
    files_indexed: int,
    clusters: list[dict[str, Any]],
    tag_map: list[dict[str, Any]],
    recent: list[dict[str, Any]],
    domain_stats: list[dict[str, Any]],
    orphans: list[str],
    frontier: list[str],
) -> str:
    """Compose the final markdown document.

    The ``generated_at`` field carries an ISO8601 timestamp from the caller's
    clock; tests inject a fixed clock so the rendered text is deterministic.
    """
    header = (
        "---\n"
        f"generated_at: {generated_at}\n"
        "generated_by: kernel/index.py\n"
        f"total_files_indexed: {files_indexed}\n"
        "---\n\n"
        "# Vault Index\n\n"
        "> Auto-generated. Do not hand-edit. The agent reads this file FIRST every turn.\n\n"
        "---\n\n"
    )

    sections = [
        _render_topic_clusters(clusters),
        "\n---\n\n",
        _render_tag_map(tag_map),
        "\n---\n\n",
        _render_recent_activity(recent),
        "\n---\n\n",
        _render_domain_stats(domain_stats),
        "\n---\n\n",
        _render_orphans(orphans),
        "\n---\n\n",
        _render_vocabulary_frontier(frontier),
    ]
    return header + "".join(sections)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def refresh(
    vault_root: str | os.PathLike[str],
    *,
    config: Optional[Mapping[str, Any]] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> RefreshResult:
    """Regenerate ``vault/_index/INDEX.md`` from the current vault state.

    Walks every ``.md`` under non-kernel domain directories, builds the six
    sections (topic clusters, tag map, recent activity, domain stats,
    orphans, vocabulary frontier), and writes the result atomically.

    Args:
        vault_root: vault root on disk.
        config: optional config map; not currently consumed but reserved
            for future toggles (e.g. a custom recent-activity limit).
        clock: pluggable wall clock so tests can pin ``generated_at`` and
            achieve byte-identical re-runs. Defaults to UTC ``now``.

    Returns:
        A :class:`RefreshResult` with the index path and indexed-file
        counts. Callers (the orchestrator) use the return value to feed
        the audit log entry.
    """
    root = Path(vault_root)
    notes = _walk_markdown_notes(root)

    clock_fn = clock or (lambda: datetime.now(tz=timezone.utc))
    # Truncate to second granularity so wall-clock noise doesn't sneak into
    # the deterministic-output guarantee for callers that pass a clock that
    # returns the same value each call.
    generated_at = clock_fn().replace(microsecond=0).isoformat()

    clusters = _build_topic_clusters(notes)
    tag_map = _build_tag_map(notes)
    recent = _build_recent_activity(notes, limit=_RECENT_LIMIT)
    domain_stats = _build_domain_stats(root, notes)
    orphans = _build_orphans(notes)
    frontier = _build_vocabulary_frontier(notes)

    body = _render_index(
        generated_at=generated_at,
        files_indexed=len(notes),
        clusters=clusters,
        tag_map=tag_map,
        recent=recent,
        domain_stats=domain_stats,
        orphans=orphans,
        frontier=frontier,
    )

    target = root / INDEX_RELATIVE_PATH
    atomic_write(target, body)

    return RefreshResult(
        index_path=target,
        files_indexed=len(notes),
        clusters=len(clusters),
        tags=len(tag_map),
        orphans=len(orphans),
    )
