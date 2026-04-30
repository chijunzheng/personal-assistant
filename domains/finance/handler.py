"""Finance plugin — write (statement extraction) + read (query_finance).

The handler exposes three callables:

  - ``write(intent, message, session, ...)`` -> ``FinanceWriteResult``.
    Extracts transactions from the statement text via a pluggable
    ``extractor`` (default: shells to ``claude_runner`` with
    ``prompt.md`` as the system prompt) and appends one JSONL row per
    transaction to ``vault/finance/transactions.jsonl``. Idempotent on
    the row's ``id`` field — a sha256 of ``date|amount|merchant|raw``.

  - ``read(intent, query, ...)`` -> ``FinanceReadResult``. Parses a
    natural-language ``finance.query`` into a structured
    ``query_finance`` invocation (via a pluggable ``query_parser`` or by
    shelling to ``claude_runner``) and returns a numeric answer plus a
    one-sentence reply.

  - ``query_finance(category, date_range, agg)`` -> ``dict``. Pure-Python
    aggregation over the JSONL — never an LLM hand-sum. ``agg`` is one
    of ``sum`` | ``count`` | ``list``. Exposed as a callable so issue
    #10 can register it in the retrieval tool palette when
    ``per_domain_shaping=true``.

The plugin is log-silent (CLAUDE.md): it never writes to ``vault/_audit``;
the kernel does that after ``write``/``read`` returns. Writes go through
``kernel.vault.atomic_write`` so a Drive sync mid-append never observes a
partial line.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from kernel.claude_runner import ClaudeResponse, invoke as claude_invoke
from kernel.session import Session
from kernel.vault import atomic_write

__all__ = [
    "FinanceReadResult",
    "FinanceWriteResult",
    "query_finance",
    "read",
    "transaction_id",
    "write",
]


# Path of the canonical transactions log inside the vault.
_TRANSACTIONS_RELATIVE = Path("finance") / "transactions.jsonl"

# Schema fields persisted on each row (matches domains/finance/domain.yaml).
# ``id`` is appended on top of these.
_PERSISTED_FIELDS: tuple[str, ...] = (
    "id",
    "date",
    "amount",
    "currency",
    "merchant",
    "merchant_normalized",
    "category",
    "subcategory",
    "source",
    "raw",
    "tags",
    "confidence",
)

# Intents this handler accepts.
_WRITE_INTENT = "finance.transaction"
_QUERY_INTENT = "finance.query"

# Default currency when the extractor doesn't supply one.
_DEFAULT_CURRENCY = "CAD"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinanceWriteResult:
    """Return value of ``write`` — what the kernel needs to audit-log + reply.

    Attributes:
        intent: registered intent label (always ``finance.transaction``).
        path: the canonical transactions JSONL path.
        appended: how many rows were added by *this* call (deduped by id).
        skipped: how many extracted rows were already on disk.
        ids: the ids of every extracted row (appended OR skipped) in
            input order.
    """

    intent: str
    path: Path
    appended: int
    skipped: int
    ids: tuple[str, ...]


@dataclass(frozen=True)
class FinanceReadResult:
    """Return value of ``read`` — numeric answer plus a human-readable reply.

    Attributes:
        intent: registered intent label (always ``finance.query``).
        reply_text: the one-sentence reply for the user.
        agg: which aggregation ran (``sum`` | ``count`` | ``list``).
        category: category that was queried.
        date_range: tuple of ``(start_iso, end_iso)`` inclusive.
        value: the numeric result (sum total, count, or 0 for ``list``).
        count: number of rows that matched the filter.
    """

    intent: str
    reply_text: str
    agg: str
    category: str
    date_range: tuple[str, str]
    value: float
    count: int


# ---------------------------------------------------------------------------
# Pluggable callable shapes
# ---------------------------------------------------------------------------


class _Extractor(Protocol):
    """Maps statement text -> a list of partial transaction dicts.

    Each returned dict should carry at least ``date``, ``amount``,
    ``merchant``, and ``raw``. Other fields (``currency``, ``category``,
    ``confidence``, ...) are filled in from defaults if absent.
    """

    def __call__(self, text: str) -> list[dict]: ...


class _ClaudeInvoker(Protocol):
    """The subset of ``claude_runner.invoke`` the handler uses."""

    def __call__(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> ClaudeResponse: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def transaction_id(*, date: str, amount: float, merchant: str, raw: str) -> str:
    """Stable sha256 over the four fields that uniquely identify a transaction.

    The PRD's idempotency invariant: every append uses a sha256 of the
    content as its id. Re-running the same extraction is a safe no-op
    because the canonical id collides on disk.
    """
    serialized = f"{date}|{amount}|{merchant}|{raw}"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _normalize_amount(value: Any) -> float:
    """Coerce ``value`` to a Python float; tolerate strings like ``-4.75``."""
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).strip())


def _normalize_row(raw_row: Mapping[str, Any], *, source: str) -> dict:
    """Fill defaults + compute the id for one extractor row.

    Returns a fully-shaped dict matching ``_PERSISTED_FIELDS``.
    """
    date = str(raw_row["date"])
    amount = _normalize_amount(raw_row["amount"])
    merchant = str(raw_row["merchant"])
    raw_line = str(raw_row.get("raw") or f"{date} {merchant} {amount}")

    row = {
        "id": transaction_id(
            date=date, amount=amount, merchant=merchant, raw=raw_line
        ),
        "date": date,
        "amount": amount,
        "currency": str(raw_row.get("currency") or _DEFAULT_CURRENCY),
        "merchant": merchant,
        "merchant_normalized": str(
            raw_row.get("merchant_normalized") or merchant.title()
        ),
        "category": str(raw_row.get("category") or "uncategorized"),
        "subcategory": raw_row.get("subcategory"),
        "source": source,
        "raw": raw_line,
        "tags": list(raw_row.get("tags") or []),
        "confidence": float(raw_row.get("confidence") or 1.0),
    }
    return row


def _existing_ids(path: Path) -> set[str]:
    """Read every existing row's id; tolerate a missing or empty file."""
    if not path.exists():
        return set()
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                # Tolerate a partial line — the next idempotent write will
                # converge regardless.
                continue
            row_id = row.get("id")
            if isinstance(row_id, str):
                seen.add(row_id)
    return seen


def _append_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Append ``rows`` to the JSONL file via the vault's atomic-write seam.

    Implementation note: append-only event logs don't strictly need
    atomic_write, but going through it routes Drive sync the same way
    every other vault write does — fewer code paths to reason about.
    """
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")

    new_lines = [json.dumps(dict(row), default=str) for row in rows]
    suffix = "\n".join(new_lines) + ("\n" if new_lines else "")

    if existing and not existing.endswith("\n"):
        existing = existing + "\n"

    atomic_write(path, existing + suffix)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _default_extractor(invoker: _ClaudeInvoker) -> _Extractor:
    """Build an extractor backed by the LLM via ``claude_runner.invoke``.

    The LLM is asked to return a JSON array of transaction dicts. Any
    failure to parse is surfaced as an empty list — the orchestrator
    audit-logs the empty result and the user sees a "0 rows extracted"
    reply, never a crashed turn.
    """
    system = _load_prompt()

    def _extract(text: str) -> list[dict]:
        prompt = (
            "Extract transactions from this statement text. "
            "Return a JSON array; each row has date (ISO8601), "
            "amount (negative=expense), currency, merchant, "
            "merchant_normalized, category, subcategory, raw, "
            "tags (array), confidence (0-1). No surrounding prose.\n\n"
            f"---\n{text}\n---\n"
        )
        response = invoker(prompt, system_prompt=system or None)
        return _parse_extractor_payload(response.text)

    return _extract


_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_prompt() -> str:
    """Read the finance prompt; fall back to empty if absent (tests)."""
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _parse_extractor_payload(text: str) -> list[dict]:
    """Decode the LLM's JSON-array response; tolerate code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip ``` fences with optional language tag.
        lines = [ln for ln in cleaned.splitlines() if not ln.startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def write(
    *,
    intent: str,
    message: str,
    session: Session,
    vault_root: str | os.PathLike[str],
    clock: Optional[Callable[[], datetime]] = None,
    extractor: Optional[_Extractor] = None,
    invoker: Optional[_ClaudeInvoker] = None,
    source: Optional[str] = None,
) -> FinanceWriteResult:
    """Persist transactions extracted from statement text.

    Args:
        intent: must be ``finance.transaction``; other values raise
            ``ValueError`` so the orchestrator's dispatch table cannot
            silently misroute.
        message: the statement text. The orchestrator pre-extracts text
            from PDFs/images upstream; the handler operates on a string.
        session: active session — surfaces ``session_id`` to the audit
            line via the ``source`` field default.
        vault_root: vault root on disk.
        clock: pluggable clock (test seam).
        extractor: pluggable transaction extractor; defaults to a
            claude_runner-backed extractor for production callers.
        invoker: passed to the default extractor when ``extractor`` is
            omitted; tests inject their own.
        source: human-readable provenance for the rows (statement file
            name, etc.); defaults to ``session.session_id`` so writes are
            still attributable.

    Returns:
        ``FinanceWriteResult`` with idempotency counts plus the row ids.

    Raises:
        ValueError: ``intent`` is not ``finance.transaction``.
    """
    if intent != _WRITE_INTENT:
        raise ValueError(
            f"finance.write only handles {_WRITE_INTENT}, not {intent!r}"
        )

    # ``clock`` is accepted to mirror the journal handler's signature even
    # though the v1 finance row doesn't carry an extraction timestamp.
    # Future fields (e.g. ``extracted_at``) can use it without an API change.
    del clock

    extract = extractor or _default_extractor(invoker or claude_invoke)
    raw_rows = extract(message) or []

    provenance = source or session.session_id
    normalized = [_normalize_row(r, source=provenance) for r in raw_rows]

    txn_path = Path(vault_root) / _TRANSACTIONS_RELATIVE
    seen = _existing_ids(txn_path)

    fresh: list[dict] = []
    seen_in_batch: set[str] = set()
    skipped = 0
    for row in normalized:
        if row["id"] in seen or row["id"] in seen_in_batch:
            skipped += 1
            continue
        fresh.append(row)
        seen_in_batch.add(row["id"])

    if fresh:
        _append_jsonl(txn_path, fresh)

    return FinanceWriteResult(
        intent=intent,
        path=txn_path,
        appended=len(fresh),
        skipped=skipped,
        ids=tuple(r["id"] for r in normalized),
    )


# ---------------------------------------------------------------------------
# query_finance — pure-Python aggregation
# ---------------------------------------------------------------------------


def _iter_rows(path: Path) -> Iterable[dict]:
    """Yield every row in the transactions JSONL, skipping blank/bad lines."""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _row_matches_category(row: Mapping[str, Any], category: str) -> bool:
    """A row matches when ``category`` equals either ``category`` or ``subcategory``.

    The convention: ``category`` in the query is matched permissively so
    the user can ask "coffee" without knowing whether the extractor
    classified it as a category or a subcategory.
    """
    target = category.strip().lower()
    cat = str(row.get("category") or "").strip().lower()
    sub = str(row.get("subcategory") or "").strip().lower()
    return target in (cat, sub)


def _row_in_range(row: Mapping[str, Any], start: str, end: str) -> bool:
    """Return True if the row's ``date`` falls inside ``[start, end]`` inclusive."""
    row_date = str(row.get("date") or "")
    return start <= row_date <= end


def query_finance(
    *,
    category: str,
    date_range: tuple[str, str],
    agg: str,
    vault_root: str | os.PathLike[str],
) -> dict:
    """Aggregate transactions matching ``category`` within ``date_range``.

    Args:
        category: matched against either ``category`` or ``subcategory``.
        date_range: ``(start_iso, end_iso)`` inclusive on both ends.
        agg: ``sum`` | ``count`` | ``list``.
        vault_root: vault root on disk; missing path is non-fatal.

    Returns:
        A dict with at least ``agg``, ``category``, ``date_range``,
        ``count``, ``value`` (numeric for sum/count, 0 for list), and —
        when ``agg=list`` — a ``rows`` list of the matching transactions.
    """
    if agg not in ("sum", "count", "list"):
        raise ValueError(f"unsupported agg {agg!r}; expected sum|count|list")

    start, end = date_range
    txn_path = Path(vault_root) / _TRANSACTIONS_RELATIVE

    matching: list[dict] = []
    for row in _iter_rows(txn_path):
        if not _row_matches_category(row, category):
            continue
        if not _row_in_range(row, start, end):
            continue
        matching.append(row)

    if agg == "sum":
        total = sum(_normalize_amount(r.get("amount", 0)) for r in matching)
        return {
            "agg": "sum",
            "category": category,
            "date_range": date_range,
            "count": len(matching),
            "value": total,
        }
    if agg == "count":
        return {
            "agg": "count",
            "category": category,
            "date_range": date_range,
            "count": len(matching),
            "value": len(matching),
        }
    # agg == "list"
    return {
        "agg": "list",
        "category": category,
        "date_range": date_range,
        "count": len(matching),
        "value": 0,
        "rows": list(matching),
    }


# ---------------------------------------------------------------------------
# read — natural-language query -> structured aggregation -> reply
# ---------------------------------------------------------------------------


_PARSE_SYSTEM_HINT = (
    "Parse a finance query into JSON: "
    '{"category": <string>, "date_range": [<start_iso>, <end_iso>], '
    '"agg": "sum" | "count" | "list"}. '
    "Respond with JSON only — no prose."
)


def _parse_query_via_invoker(invoker: _ClaudeInvoker, query: str) -> dict:
    """Ask the LLM to map a free-form question onto the query_finance shape."""
    response = invoker(query, system_prompt=_PARSE_SYSTEM_HINT)
    text = response.text.strip()
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.startswith("```")]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _coerce_parsed_query(parsed: Mapping[str, Any]) -> dict:
    """Coerce the parser output into the kwargs ``query_finance`` expects."""
    category = str(parsed.get("category") or "").strip()
    raw_range = parsed.get("date_range") or ("", "")
    if isinstance(raw_range, (list, tuple)) and len(raw_range) >= 2:
        start, end = str(raw_range[0]), str(raw_range[1])
    else:
        start, end = "", ""
    agg = str(parsed.get("agg") or "sum")
    return {"category": category, "date_range": (start, end), "agg": agg}


def _format_amount(value: float, currency: str) -> str:
    """Render an amount as ``$X.XX`` (or ``-$X.XX`` for negatives)."""
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.2f} {currency}".strip()


def _build_reply(
    *,
    category: str,
    date_range: tuple[str, str],
    agg: str,
    count: int,
    value: float,
    sample_rows: Optional[list[dict]] = None,
) -> str:
    """Produce a one-sentence numeric answer plus a context line.

    The phrasing is intentionally plain so the user can sanity-check the
    number against the JSONL.
    """
    start, end = date_range
    if count == 0:
        return (
            f"No {category} transactions found between {start} and {end}."
        )

    if agg == "sum":
        currency = "CAD"
        if sample_rows:
            currency = sample_rows[0].get("currency") or currency
        return (
            f"You spent {_format_amount(value, currency)} on {category} "
            f"across {count} transaction{'s' if count != 1 else ''} "
            f"({start} to {end})."
        )
    if agg == "count":
        return (
            f"{count} {category} transaction{'s' if count != 1 else ''} "
            f"between {start} and {end}."
        )
    # list
    return (
        f"{count} {category} transaction{'s' if count != 1 else ''} "
        f"between {start} and {end}; see the structured rows for detail."
    )


def read(
    *,
    intent: str,
    query: str,
    vault_root: str | os.PathLike[str],
    query_parser: Optional[Callable[[str], Mapping[str, Any]]] = None,
    invoker: Optional[_ClaudeInvoker] = None,
) -> FinanceReadResult:
    """Answer a ``finance.query`` with a real numeric aggregation.

    Args:
        intent: must be ``finance.query``.
        query: the user's free-form question.
        vault_root: vault root on disk.
        query_parser: pluggable mapper from query text to a dict with
            ``category`` + ``date_range`` + ``agg``. Tests inject one
            so the unit test doesn't shell to the LLM.
        invoker: pluggable ``claude_runner.invoke`` used when
            ``query_parser`` is not supplied.

    Returns:
        ``FinanceReadResult`` with the numeric answer and a reply string.

    Raises:
        ValueError: ``intent`` is not ``finance.query``.
    """
    if intent != _QUERY_INTENT:
        raise ValueError(
            f"finance.read only handles {_QUERY_INTENT}, not {intent!r}"
        )

    if query_parser is not None:
        parsed = query_parser(query) or {}
    else:
        parsed = _parse_query_via_invoker(invoker or claude_invoke, query)

    coerced = _coerce_parsed_query(parsed)
    category = coerced["category"]
    date_range = coerced["date_range"]
    agg = coerced["agg"]

    if agg not in ("sum", "count", "list"):
        agg = "sum"

    aggregated = query_finance(
        category=category,
        date_range=date_range,
        agg=agg,
        vault_root=vault_root,
    )

    rows_for_currency = aggregated.get("rows") if agg == "list" else None
    if rows_for_currency is None and agg == "sum":
        # Pull a single row to surface currency in the reply when summing.
        sample = list(_iter_rows(Path(vault_root) / _TRANSACTIONS_RELATIVE))
        rows_for_currency = [
            r for r in sample if _row_matches_category(r, category)
        ][:1]

    reply = _build_reply(
        category=category,
        date_range=date_range,
        agg=agg,
        count=aggregated["count"],
        value=aggregated["value"],
        sample_rows=rows_for_currency,
    )

    return FinanceReadResult(
        intent=intent,
        reply_text=reply,
        agg=agg,
        category=category,
        date_range=date_range,
        value=float(aggregated["value"]),
        count=int(aggregated["count"]),
    )
