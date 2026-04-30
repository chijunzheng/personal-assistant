"""Tests for ``domains.finance.handler`` — write + read + query_finance.

The finance plugin:

  - ``write(intent, message_or_attachment, session)`` parses statement text
    (already extracted by the caller, or via a pluggable extractor) into
    transactions and appends them idempotently to
    ``vault/finance/transactions.jsonl``.
  - ``read(intent, query, context_bundle)`` answers structured queries by
    parsing the natural-language question into a ``query_finance`` call
    and returning the numeric answer.
  - ``query_finance(category, date_range, agg)`` is a callable utility
    that runs real Python arithmetic over the JSONL — never an LLM
    hand-sum.

Idempotency is the load-bearing invariant: re-uploading the same
statement adds zero new rows because the row id is
``sha256(date|amount|merchant|raw)``.

The ``claude_runner`` is mocked everywhere; tests use a fixed clock and a
temp vault.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from domains.finance.handler import (
    FinanceWriteResult,
    query_finance,
    read,
    transaction_id,
    write,
)
from kernel.claude_runner import ClaudeResponse
from kernel.session import Session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_session(chat_id: str = "chat-1", session_id: str = "sess-fin") -> Session:
    """Build a minimal Session for handler tests."""
    return Session(
        chat_id=chat_id,
        session_id=session_id,
        started_at="2026-04-29T10:00:00+00:00",
        last_updated="2026-04-29T10:00:00+00:00",
        turns=0,
        summary="",
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 29, 12, 30, 0, tzinfo=timezone.utc)


_SAMPLE_TXNS = [
    {
        "date": "2026-03-04",
        "amount": -4.75,
        "currency": "CAD",
        "merchant": "STARBUCKS #1234",
        "merchant_normalized": "Starbucks",
        "category": "food",
        "subcategory": "coffee",
        "raw": "2026-03-04 STARBUCKS #1234 CAD -4.75",
        "tags": [],
        "confidence": 0.95,
    },
    {
        "date": "2026-03-12",
        "amount": -3.50,
        "currency": "CAD",
        "merchant": "TIM HORTONS",
        "merchant_normalized": "Tim Hortons",
        "category": "food",
        "subcategory": "coffee",
        "raw": "2026-03-12 TIM HORTONS CAD -3.50",
        "tags": [],
        "confidence": 0.92,
    },
    {
        "date": "2026-03-20",
        "amount": -85.40,
        "currency": "CAD",
        "merchant": "LOBLAWS",
        "merchant_normalized": "Loblaws",
        "category": "groceries",
        "subcategory": None,
        "raw": "2026-03-20 LOBLAWS CAD -85.40",
        "tags": [],
        "confidence": 0.97,
    },
    {
        "date": "2026-04-02",
        "amount": -5.25,
        "currency": "CAD",
        "merchant": "STARBUCKS #5678",
        "merchant_normalized": "Starbucks",
        "category": "food",
        "subcategory": "coffee",
        "raw": "2026-04-02 STARBUCKS #5678 CAD -5.25",
        "tags": [],
        "confidence": 0.95,
    },
]


def _stub_extractor(rows: list[dict]):
    """Build a pluggable extractor that returns a fixed transaction list."""

    def _extract(_text: str) -> list[dict]:
        return [dict(r) for r in rows]

    return _extract


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# transaction_id — deterministic content hash
# ---------------------------------------------------------------------------


def test_transaction_id_is_stable_for_same_inputs() -> None:
    """Two calls with identical date/amount/merchant/raw yield identical ids."""
    a = transaction_id(
        date="2026-03-04",
        amount=-4.75,
        merchant="STARBUCKS #1234",
        raw="2026-03-04 STARBUCKS #1234 CAD -4.75",
    )
    b = transaction_id(
        date="2026-03-04",
        amount=-4.75,
        merchant="STARBUCKS #1234",
        raw="2026-03-04 STARBUCKS #1234 CAD -4.75",
    )
    assert a == b
    # 64 hex chars = full sha256
    assert len(a) == 64


def test_transaction_id_differs_when_any_field_differs() -> None:
    """Changing any one of date/amount/merchant/raw changes the id."""
    base = dict(
        date="2026-03-04",
        amount=-4.75,
        merchant="STARBUCKS #1234",
        raw="2026-03-04 STARBUCKS #1234 CAD -4.75",
    )
    base_id = transaction_id(**base)

    different_date = transaction_id(**{**base, "date": "2026-03-05"})
    different_amount = transaction_id(**{**base, "amount": -4.50})
    different_merchant = transaction_id(**{**base, "merchant": "STARBUCKS #9999"})
    different_raw = transaction_id(**{**base, "raw": "(other)"})

    assert base_id != different_date
    assert base_id != different_amount
    assert base_id != different_merchant
    assert base_id != different_raw


# ---------------------------------------------------------------------------
# write — extracts + appends + is idempotent
# ---------------------------------------------------------------------------


def test_write_extracts_and_appends_rows_to_jsonl(tmp_path: Path) -> None:
    """A fresh statement upload appends one row per extracted transaction."""
    vault_root = tmp_path / "vault"

    result = write(
        intent="finance.transaction",
        message="(statement text irrelevant; extractor is stubbed)",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(_SAMPLE_TXNS),
        source="march-statement.pdf",
    )

    assert isinstance(result, FinanceWriteResult)
    txn_file = vault_root / "finance" / "transactions.jsonl"
    rows = _read_jsonl(txn_file)
    assert len(rows) == len(_SAMPLE_TXNS)
    # Every row has the schema fields from domain.yaml
    for row in rows:
        assert set(row.keys()) >= {
            "id",
            "date",
            "amount",
            "currency",
            "merchant",
            "merchant_normalized",
            "category",
            "source",
            "raw",
            "tags",
            "confidence",
        }
    # Result reports how many rows were appended this call.
    assert result.appended == len(_SAMPLE_TXNS)
    assert result.skipped == 0


def test_write_is_idempotent_on_duplicate_upload(tmp_path: Path) -> None:
    """Re-running the same extraction adds 0 new rows."""
    vault_root = tmp_path / "vault"

    write(
        intent="finance.transaction",
        message="statement",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(_SAMPLE_TXNS),
        source="march-statement.pdf",
    )
    second = write(
        intent="finance.transaction",
        message="statement",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(_SAMPLE_TXNS),
        source="march-statement.pdf",
    )

    rows = _read_jsonl(vault_root / "finance" / "transactions.jsonl")
    assert len(rows) == len(_SAMPLE_TXNS)
    assert second.appended == 0
    assert second.skipped == len(_SAMPLE_TXNS)


def test_write_partial_overlap_only_appends_new_rows(tmp_path: Path) -> None:
    """If a follow-up statement overlaps prior rows, only the genuinely-new ones land."""
    vault_root = tmp_path / "vault"

    write(
        intent="finance.transaction",
        message="first batch",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(_SAMPLE_TXNS[:2]),  # rows 0 + 1
        source="march-statement.pdf",
    )
    second = write(
        intent="finance.transaction",
        message="second batch overlaps row 1",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(_SAMPLE_TXNS[1:]),  # rows 1 + 2 + 3
        source="march-statement.pdf",
    )

    rows = _read_jsonl(vault_root / "finance" / "transactions.jsonl")
    assert len(rows) == 4  # one duplicate skipped
    assert second.appended == 2
    assert second.skipped == 1


def test_write_assigns_sha256_id_for_each_row(tmp_path: Path) -> None:
    """Each persisted row carries a 64-hex sha256 id."""
    vault_root = tmp_path / "vault"

    write(
        intent="finance.transaction",
        message="statement",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(_SAMPLE_TXNS),
        source="march-statement.pdf",
    )

    rows = _read_jsonl(vault_root / "finance" / "transactions.jsonl")
    for row in rows:
        assert isinstance(row["id"], str)
        assert len(row["id"]) == 64
        # sha256 hex is [0-9a-f]
        int(row["id"], 16)  # raises if non-hex


def test_write_rejects_wrong_intent(tmp_path: Path) -> None:
    """Only ``finance.transaction`` is a valid write intent here."""
    vault_root = tmp_path / "vault"
    try:
        write(
            intent="finance.query",
            message="how much on coffee?",
            session=_make_session(),
            vault_root=vault_root,
            clock=_fixed_clock,
            extractor=_stub_extractor(_SAMPLE_TXNS),
            source="x",
        )
    except ValueError:
        return
    raise AssertionError("write should reject a non-write intent")


# ---------------------------------------------------------------------------
# query_finance — real Python arithmetic over JSONL
# ---------------------------------------------------------------------------


def _seed_transactions(vault_root: Path) -> Path:
    """Drop the sample transactions on disk for query tests."""
    finance = vault_root / "finance"
    finance.mkdir(parents=True, exist_ok=True)
    txn_file = finance / "transactions.jsonl"
    with open(txn_file, "w", encoding="utf-8") as fh:
        for row in _SAMPLE_TXNS:
            stamped = dict(row)
            stamped["id"] = transaction_id(
                date=row["date"],
                amount=row["amount"],
                merchant=row["merchant"],
                raw=row["raw"],
            )
            stamped["source"] = "test"
            fh.write(json.dumps(stamped) + "\n")
    return txn_file


def test_query_finance_sums_coffee_in_march(tmp_path: Path) -> None:
    """Sum of coffee transactions in March equals the manually computed total."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root)

    result = query_finance(
        category="coffee",
        date_range=("2026-03-01", "2026-03-31"),
        agg="sum",
        vault_root=vault_root,
    )

    # Manual addition: -4.75 + -3.50 = -8.25
    assert result["agg"] == "sum"
    assert result["category"] == "coffee"
    assert result["count"] == 2
    assert round(result["value"], 2) == -8.25


def test_query_finance_count_groceries(tmp_path: Path) -> None:
    """Counting groceries in March returns exactly 1."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root)

    result = query_finance(
        category="groceries",
        date_range=("2026-03-01", "2026-03-31"),
        agg="count",
        vault_root=vault_root,
    )

    assert result["agg"] == "count"
    assert result["value"] == 1
    assert result["count"] == 1


def test_query_finance_list_returns_matching_rows(tmp_path: Path) -> None:
    """``agg=list`` returns the actual matching rows for inspection."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root)

    result = query_finance(
        category="coffee",
        date_range=("2026-03-01", "2026-04-30"),
        agg="list",
        vault_root=vault_root,
    )

    assert result["agg"] == "list"
    assert result["count"] == 3  # two march coffees + one april coffee
    merchants = sorted(r["merchant_normalized"] for r in result["rows"])
    assert merchants == ["Starbucks", "Starbucks", "Tim Hortons"]


def test_query_finance_handles_no_matches_gracefully(tmp_path: Path) -> None:
    """A category that doesn't exist returns 0/empty, not an exception."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root)

    result = query_finance(
        category="entertainment",
        date_range=("2026-03-01", "2026-03-31"),
        agg="sum",
        vault_root=vault_root,
    )
    assert result["count"] == 0
    assert result["value"] == 0


def test_query_finance_returns_empty_when_no_jsonl_file(tmp_path: Path) -> None:
    """Querying before any transactions exist degrades gracefully."""
    vault_root = tmp_path / "vault"
    result = query_finance(
        category="coffee",
        date_range=("2026-03-01", "2026-03-31"),
        agg="sum",
        vault_root=vault_root,
    )
    assert result["count"] == 0
    assert result["value"] == 0


def test_query_finance_filters_by_subcategory_when_category_matches_subcategory(
    tmp_path: Path,
) -> None:
    """``coffee`` resolves against either ``category`` or ``subcategory``."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root)
    # The seed has coffee tagged as subcategory; the query convention should
    # accept that as a coffee match.
    result = query_finance(
        category="coffee",
        date_range=("2026-04-01", "2026-04-30"),
        agg="sum",
        vault_root=vault_root,
    )
    assert result["count"] == 1
    assert round(result["value"], 2) == -5.25


# ---------------------------------------------------------------------------
# read — natural-language query -> query_finance -> reply text
# ---------------------------------------------------------------------------


def _stub_invoker_returning(text: str):
    """A claude_runner invoker that returns a fixed text payload."""

    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=1,
            tokens_out=1,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def test_read_parses_query_and_returns_real_number(tmp_path: Path) -> None:
    """A finance.query produces a reply that contains the structured numeric answer."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root)

    # The query parser is pluggable so unit tests don't shell to claude.
    def _parser(_query: str) -> dict:
        return {
            "category": "coffee",
            "date_range": ("2026-03-01", "2026-03-31"),
            "agg": "sum",
        }

    result = read(
        intent="finance.query",
        query="how much did I spend on coffee last month?",
        vault_root=vault_root,
        query_parser=_parser,
    )

    assert "8.25" in result.reply_text
    assert result.value is not None
    assert round(result.value, 2) == -8.25
    assert result.count == 2
    assert result.agg == "sum"


def test_read_includes_query_summary_for_human_context(tmp_path: Path) -> None:
    """Reply text mentions category + date range + count for trust signal."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root)

    def _parser(_query: str) -> dict:
        return {
            "category": "coffee",
            "date_range": ("2026-03-01", "2026-03-31"),
            "agg": "sum",
        }

    result = read(
        intent="finance.query",
        query="coffee in march?",
        vault_root=vault_root,
        query_parser=_parser,
    )
    assert "coffee" in result.reply_text.lower()
    # Either the date range or "2 transaction" should make the answer auditable.
    assert "2026-03" in result.reply_text or "2 " in result.reply_text


def test_read_rejects_wrong_intent(tmp_path: Path) -> None:
    """Only ``finance.query`` is a valid read intent."""
    vault_root = tmp_path / "vault"
    try:
        read(
            intent="finance.transaction",
            query="(unused)",
            vault_root=vault_root,
            query_parser=lambda _q: {},
        )
    except ValueError:
        return
    raise AssertionError("read should reject a non-query intent")


def test_read_handles_no_matches(tmp_path: Path) -> None:
    """When the query yields zero matches the reply explains so plainly."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root)

    def _parser(_query: str) -> dict:
        return {
            "category": "entertainment",
            "date_range": ("2026-03-01", "2026-03-31"),
            "agg": "sum",
        }

    result = read(
        intent="finance.query",
        query="entertainment last march?",
        vault_root=vault_root,
        query_parser=_parser,
    )
    assert result.value == 0
    assert result.count == 0
    assert (
        "no" in result.reply_text.lower()
        or "0" in result.reply_text
    )


def test_read_uses_invoker_to_parse_when_no_parser_supplied(tmp_path: Path) -> None:
    """Without an explicit parser, ``read`` shells to ``claude_runner`` for parsing."""
    vault_root = tmp_path / "vault"
    _seed_transactions(vault_root)

    invoker = _stub_invoker_returning(
        text=json.dumps(
            {
                "category": "coffee",
                "date_range": ["2026-03-01", "2026-03-31"],
                "agg": "sum",
            }
        )
    )

    result = read(
        intent="finance.query",
        query="coffee in march?",
        vault_root=vault_root,
        invoker=invoker,
    )

    assert round(result.value, 2) == -8.25
    assert result.count == 2
