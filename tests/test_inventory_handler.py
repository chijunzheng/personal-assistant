"""Tests for ``domains.inventory.handler`` — write + read + query_inventory.

The inventory plugin uses a hybrid storage model:

  - ``vault/inventory/events.jsonl`` — append-only log of bought / consumed /
    adjusted events with sha256 ``id`` fields (idempotency on identical
    natural-language re-issue).
  - ``vault/inventory/state.yaml`` — canonical "what I have now"; recomputed
    from the full event log on every successful append.

The state-recompute correctness is the load-bearing assertion: GIVEN a
sequence of bought/consumed/adjusted events, the resulting ``state.yaml``
must match the math.

The ``claude_runner`` is mocked everywhere; tests use a fixed clock and a
temp vault.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from domains.inventory.handler import (
    InventoryWriteResult,
    query_inventory,
    read,
    write,
)
from kernel.claude_runner import ClaudeResponse
from kernel.session import Session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_session(chat_id: str = "chat-1", session_id: str = "sess-inv") -> Session:
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


def _stub_extractor(payload: dict):
    """Build a pluggable extractor that returns a fixed parsed event dict."""

    def _extract(_message: str, _intent: str) -> dict:
        return dict(payload)

    return _extract


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# write — appends event + recomputes state
# ---------------------------------------------------------------------------


def test_write_appends_bought_event_to_jsonl(tmp_path: Path) -> None:
    """A fresh ``inventory.add`` writes one row to events.jsonl with a sha256 id."""
    vault_root = tmp_path / "vault"

    result = write(
        intent="inventory.add",
        message="bought 2 milks at Costco",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"item": "milk", "quantity_delta": 2, "unit": "count", "location": "fridge"}
        ),
    )

    assert isinstance(result, InventoryWriteResult)
    events_path = vault_root / "inventory" / "events.jsonl"
    rows = _read_events(events_path)
    assert len(rows) == 1
    assert rows[0]["type"] == "bought"
    assert rows[0]["item"] == "milk"
    assert rows[0]["quantity_delta"] == 2
    assert isinstance(rows[0]["id"], str)
    assert len(rows[0]["id"]) == 64
    int(rows[0]["id"], 16)  # raises if non-hex


def test_write_recomputes_state_from_event_log(tmp_path: Path) -> None:
    """state.yaml shows the new total for the item after a bought event."""
    vault_root = tmp_path / "vault"

    write(
        intent="inventory.add",
        message="bought 2 milks at Costco",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"item": "milk", "quantity_delta": 2, "unit": "count", "location": "fridge"}
        ),
    )

    state = _read_state(vault_root / "inventory" / "state.yaml")
    assert "milk" in state
    assert state["milk"]["quantity"] == 2
    assert state["milk"]["unit"] == "count"
    assert state["milk"]["location"] == "fridge"


def test_write_is_idempotent_on_identical_natural_language(tmp_path: Path) -> None:
    """Re-issuing the same message + session does not append a second event."""
    vault_root = tmp_path / "vault"

    extractor = _stub_extractor(
        {"item": "milk", "quantity_delta": 2, "unit": "count", "location": "fridge"}
    )

    first = write(
        intent="inventory.add",
        message="bought 2 milks at Costco",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=extractor,
    )
    second = write(
        intent="inventory.add",
        message="bought 2 milks at Costco",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=extractor,
    )

    rows = _read_events(vault_root / "inventory" / "events.jsonl")
    assert len(rows) == 1
    assert first.appended is True
    assert second.appended is False
    # State remains correct (still 2, not 4).
    state = _read_state(vault_root / "inventory" / "state.yaml")
    assert state["milk"]["quantity"] == 2


def test_write_consume_decrements_state_quantity(tmp_path: Path) -> None:
    """An ``inventory.consume`` event sets a negative delta and lowers state."""
    vault_root = tmp_path / "vault"

    # First buy 5.
    write(
        intent="inventory.add",
        message="bought 5 milks",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"item": "milk", "quantity_delta": 5, "unit": "count", "location": "fridge"}
        ),
    )
    # Then consume 2.
    write(
        intent="inventory.consume",
        message="used 2 milks for cereal",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"item": "milk", "quantity_delta": 2, "unit": "count", "location": "fridge"}
        ),
    )

    rows = _read_events(vault_root / "inventory" / "events.jsonl")
    assert len(rows) == 2
    assert rows[0]["type"] == "bought"
    assert rows[0]["quantity_delta"] == 5
    assert rows[1]["type"] == "consumed"
    assert rows[1]["quantity_delta"] == -2

    state = _read_state(vault_root / "inventory" / "state.yaml")
    assert state["milk"]["quantity"] == 3


def test_write_adjust_sets_canonical_quantity(tmp_path: Path) -> None:
    """An ``inventory.adjust`` event corrects state to a target quantity.

    The math: bought 5, consumed 2, adjusted to 4 => state shows 4. The
    adjust event records the delta needed to reach 4 from the current 3
    (i.e. +1) so replay from the event log is still mathematically sound.
    """
    vault_root = tmp_path / "vault"

    write(
        intent="inventory.add",
        message="bought 5 milks",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"item": "milk", "quantity_delta": 5, "unit": "count", "location": "fridge"}
        ),
    )
    write(
        intent="inventory.consume",
        message="used 2 milks",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"item": "milk", "quantity_delta": 2, "unit": "count", "location": "fridge"}
        ),
    )
    # Adjust to 4 (extractor supplies the absolute target).
    write(
        intent="inventory.adjust",
        message="actually only 4 milks left",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {
                "item": "milk",
                "target_quantity": 4,
                "unit": "count",
                "location": "fridge",
            }
        ),
    )

    state = _read_state(vault_root / "inventory" / "state.yaml")
    assert state["milk"]["quantity"] == 4

    # Replaying the events arithmetically should also produce 4.
    rows = _read_events(vault_root / "inventory" / "events.jsonl")
    total = sum(r["quantity_delta"] for r in rows if r["item"] == "milk")
    assert total == 4


def test_write_default_low_threshold_when_unspecified(tmp_path: Path) -> None:
    """When extractor omits low_threshold, state carries a sensible default."""
    vault_root = tmp_path / "vault"

    write(
        intent="inventory.add",
        message="bought 2 AAA batteries",
        session=_make_session(),
        vault_root=vault_root,
        clock=_fixed_clock,
        extractor=_stub_extractor(
            {"item": "AAA batteries", "quantity_delta": 2, "unit": "count"}
        ),
    )
    state = _read_state(vault_root / "inventory" / "state.yaml")
    assert state["AAA batteries"]["low_threshold"] >= 1


def test_write_rejects_non_inventory_intent(tmp_path: Path) -> None:
    """Only inventory.add | inventory.consume | inventory.adjust are valid here."""
    vault_root = tmp_path / "vault"
    try:
        write(
            intent="inventory.query",
            message="what's in the fridge?",
            session=_make_session(),
            vault_root=vault_root,
            clock=_fixed_clock,
            extractor=_stub_extractor({"item": "milk", "quantity_delta": 0}),
        )
    except ValueError:
        return
    raise AssertionError("write should reject a non-write intent")


# ---------------------------------------------------------------------------
# query_inventory — exact lists / values, no LLM hand-arithmetic
# ---------------------------------------------------------------------------


def _seed_state(vault_root: Path, state: dict) -> Path:
    """Drop a state.yaml directly so query tests start populated."""
    inventory = vault_root / "inventory"
    inventory.mkdir(parents=True, exist_ok=True)
    state_path = inventory / "state.yaml"
    state_path.write_text(yaml.safe_dump(state, sort_keys=True), encoding="utf-8")
    return state_path


def test_query_inventory_item_returns_exact_quantity(tmp_path: Path) -> None:
    """Mode=item returns the canonical state row for a known item."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {
            "milk": {
                "quantity": 3,
                "unit": "count",
                "location": "fridge",
                "low_threshold": 1,
            }
        },
    )

    result = query_inventory(mode="item", item="milk", vault_root=vault_root)
    assert result["item"] == "milk"
    assert result["quantity"] == 3
    assert result["unit"] == "count"
    assert result["location"] == "fridge"
    assert result["found"] is True


def test_query_inventory_item_handles_missing_item(tmp_path: Path) -> None:
    """Mode=item for an unknown item returns found=False, quantity=0."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {"milk": {"quantity": 3, "unit": "count", "low_threshold": 1}},
    )

    result = query_inventory(mode="item", item="kombucha", vault_root=vault_root)
    assert result["found"] is False
    assert result["quantity"] == 0


def test_query_inventory_low_stock_returns_items_below_threshold(tmp_path: Path) -> None:
    """Mode=low_stock surfaces items where quantity < low_threshold."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {
            "milk": {"quantity": 0, "unit": "count", "low_threshold": 1},
            "eggs": {"quantity": 12, "unit": "count", "low_threshold": 6},
            "AAA batteries": {"quantity": 1, "unit": "count", "low_threshold": 4},
        },
    )

    result = query_inventory(mode="low_stock", vault_root=vault_root)
    items = sorted(r["item"] for r in result["items"])
    assert items == ["AAA batteries", "milk"]


def test_query_inventory_list_returns_all_items(tmp_path: Path) -> None:
    """Mode=list returns the full state as a list of rows."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {
            "milk": {"quantity": 3, "unit": "count", "low_threshold": 1},
            "eggs": {"quantity": 12, "unit": "count", "low_threshold": 6},
        },
    )

    result = query_inventory(mode="list", vault_root=vault_root)
    items = sorted(r["item"] for r in result["items"])
    assert items == ["eggs", "milk"]


def test_query_inventory_returns_empty_when_no_state(tmp_path: Path) -> None:
    """Querying before any state exists degrades gracefully."""
    vault_root = tmp_path / "vault"
    result = query_inventory(mode="list", vault_root=vault_root)
    assert result["items"] == []


def test_query_inventory_rejects_unknown_mode(tmp_path: Path) -> None:
    """A bad mode raises ValueError (no silent misroute)."""
    vault_root = tmp_path / "vault"
    try:
        query_inventory(mode="elsewhere", vault_root=vault_root)
    except ValueError:
        return
    raise AssertionError("query_inventory should reject an unknown mode")


# ---------------------------------------------------------------------------
# read — natural-language query -> query_inventory dispatch -> reply
# ---------------------------------------------------------------------------


def _stub_invoker_returning(text: str):
    def _invoke(prompt, *, system_prompt: Optional[str] = None):
        return ClaudeResponse(
            text=text,
            tokens_in=1,
            tokens_out=1,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )

    return _invoke


def test_read_inventory_query_returns_reply_for_known_item(tmp_path: Path) -> None:
    """A inventory.query reply mentions the item + the real quantity."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {"milk": {"quantity": 3, "unit": "count", "low_threshold": 1}},
    )

    def _parser(_query: str) -> dict:
        return {"mode": "item", "item": "milk"}

    result = read(
        intent="inventory.query",
        query="do I still have milk?",
        vault_root=vault_root,
        query_parser=_parser,
    )
    assert "milk" in result.reply_text.lower()
    assert "3" in result.reply_text
    assert result.mode == "item"
    assert result.count == 3


def test_read_inventory_list_low_returns_running_low_items(tmp_path: Path) -> None:
    """A inventory.list_low reply lists the actual under-threshold items."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {
            "milk": {"quantity": 0, "unit": "count", "low_threshold": 1},
            "eggs": {"quantity": 12, "unit": "count", "low_threshold": 6},
        },
    )

    result = read(
        intent="inventory.list_low",
        query="what's running low?",
        vault_root=vault_root,
    )
    assert "milk" in result.reply_text.lower()
    assert "eggs" not in result.reply_text.lower()
    assert result.mode == "low_stock"
    assert result.count == 1


def test_read_inventory_query_no_match_friendly(tmp_path: Path) -> None:
    """When the queried item is unknown the reply says so plainly."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {"milk": {"quantity": 3, "unit": "count", "low_threshold": 1}},
    )

    def _parser(_query: str) -> dict:
        return {"mode": "item", "item": "kombucha"}

    result = read(
        intent="inventory.query",
        query="do I have kombucha?",
        vault_root=vault_root,
        query_parser=_parser,
    )
    text = result.reply_text.lower()
    assert "kombucha" in text
    assert "no" in text or "0" in text or "don't" in text


def test_read_rejects_wrong_intent(tmp_path: Path) -> None:
    """Only inventory.query | inventory.list_low are valid read intents."""
    vault_root = tmp_path / "vault"
    try:
        read(
            intent="inventory.add",
            query="(unused)",
            vault_root=vault_root,
            query_parser=lambda _q: {"mode": "list"},
        )
    except ValueError:
        return
    raise AssertionError("read should reject a non-query intent")


def test_read_inventory_query_uses_invoker_when_no_parser(tmp_path: Path) -> None:
    """Without a parser, ``read`` shells to claude_runner for parsing."""
    vault_root = tmp_path / "vault"
    _seed_state(
        vault_root,
        {"milk": {"quantity": 3, "unit": "count", "low_threshold": 1}},
    )

    invoker = _stub_invoker_returning(
        text=json.dumps({"mode": "item", "item": "milk"})
    )

    result = read(
        intent="inventory.query",
        query="do I have milk?",
        vault_root=vault_root,
        invoker=invoker,
    )
    assert result.count == 3
    assert "milk" in result.reply_text.lower()
