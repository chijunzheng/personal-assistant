"""Inventory plugin — write (event log + state recompute) + read (query_inventory).

The plugin lives behind a tiny surface:

  - ``write(intent, message, session, ...)`` parses one inventory event
    from natural language, appends it to ``vault/inventory/events.jsonl``
    with a content-derived sha256 ``id`` (idempotent on identical re-issue),
    and then recomputes ``vault/inventory/state.yaml`` from the full event
    log so state is always derivable from events.

  - ``read(intent, query, ...)`` dispatches an ``inventory.query`` or
    ``inventory.list_low`` to ``query_inventory`` and renders a one-sentence
    reply.

  - ``query_inventory(mode, item=None, vault_root=...)`` is a pure-Python
    state lookup over ``state.yaml``. Modes:
      - ``item``       -> exact quantity for one item
      - ``low_stock``  -> items where ``quantity < low_threshold``
      - ``list``       -> every item

The plugin is log-silent (CLAUDE.md): it never touches ``vault/_audit``.
The kernel writes audit entries after ``write``/``read`` returns. State
recompute goes through ``kernel.vault.atomic_write`` so a Drive sync
mid-update never observes a half-written file.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

import yaml

from kernel.claude_runner import ClaudeResponse, invoke as claude_invoke
from kernel.session import Session
from kernel.vault import atomic_write

__all__ = [
    "InventoryReadResult",
    "InventoryWriteResult",
    "event_id",
    "query_inventory",
    "read",
    "write",
]


# Paths inside the vault.
_EVENTS_RELATIVE = Path("inventory") / "events.jsonl"
_STATE_RELATIVE = Path("inventory") / "state.yaml"

# Intent labels understood by this handler.
_WRITE_INTENTS = ("inventory.add", "inventory.consume", "inventory.adjust")
_READ_INTENTS = ("inventory.query", "inventory.list_low")

# Maps a write intent to the persisted event ``type``.
_INTENT_TO_TYPE = {
    "inventory.add": "bought",
    "inventory.consume": "consumed",
    "inventory.adjust": "adjusted",
}

# Default low-stock threshold when the extractor doesn't supply one.
_DEFAULT_LOW_THRESHOLD = 1


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InventoryWriteResult:
    """Return value of ``write`` — what the kernel needs to audit-log + reply.

    Attributes:
        intent: the registered intent label.
        path: the canonical events JSONL path.
        state_path: where the recomputed state.yaml lives.
        event_id: sha256 id of the event row (whether or not appended).
        appended: ``True`` if a new row was added; ``False`` if this call
            was an idempotent no-op (the same id was already on disk).
        item: the canonical item name written.
        quantity_delta: the signed delta recorded in the event row.
    """

    intent: str
    path: Path
    state_path: Path
    event_id: str
    appended: bool
    item: str
    quantity_delta: float


@dataclass(frozen=True)
class InventoryReadResult:
    """Return value of ``read`` — the rendered reply plus structured fields.

    Attributes:
        intent: the registered intent label.
        reply_text: the one-sentence reply for the user.
        mode: which ``query_inventory`` mode ran.
        item: the queried item, when ``mode='item'``; otherwise empty.
        count: the numeric answer (quantity for item, len(items) otherwise).
    """

    intent: str
    reply_text: str
    mode: str
    item: str
    count: float


# ---------------------------------------------------------------------------
# Pluggable callable shapes
# ---------------------------------------------------------------------------


class _Extractor(Protocol):
    """Maps (message, intent) -> a parsed event dict.

    Returned dict carries at minimum ``item`` and either ``quantity_delta``
    (for add/consume) or ``target_quantity`` (for adjust). Optional fields:
    ``unit``, ``location``, ``low_threshold``, ``tags``.
    """

    def __call__(self, message: str, intent: str) -> Mapping[str, Any]: ...


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


def event_id(*, intent: str, message: str, session_id: str) -> str:
    """Stable sha256 over the inputs that uniquely identify a re-issue.

    The PRD's idempotency invariant: every append uses a sha256 of the
    content as its id. Re-running the same intent + message + session id
    is a safe no-op because the canonical id collides on disk.
    """
    serialized = f"{intent}\n{session_id}\n{message}"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _now(clock: Optional[Callable[[], datetime]] = None) -> datetime:
    return (clock or (lambda: datetime.now(tz=timezone.utc)))()


def _normalize_quantity(value: Any) -> float:
    """Coerce a quantity-ish value to ``float``."""
    if isinstance(value, bool):
        # bool is a subclass of int — guard against accidental truthiness.
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).strip())


def _iter_events(path: Path) -> Iterable[dict]:
    """Yield each event row from the JSONL log; skip blank/bad lines."""
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


def _existing_ids(path: Path) -> set[str]:
    """Return every existing event id; tolerate a missing file."""
    seen: set[str] = set()
    for row in _iter_events(path):
        row_id = row.get("id")
        if isinstance(row_id, str):
            seen.add(row_id)
    return seen


def _append_event(path: Path, event: Mapping[str, Any]) -> None:
    """Append one event row to the JSONL via the atomic-write seam."""
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing = existing + "\n"
    line = json.dumps(dict(event), default=str) + "\n"
    atomic_write(path, existing + line)


def _build_event(
    *,
    parsed: Mapping[str, Any],
    intent: str,
    message: str,
    session_id: str,
    timestamp: str,
    eid: str,
) -> dict:
    """Shape one extractor row into a persisted event row.

    For ``inventory.adjust`` the extractor supplies ``target_quantity``; the
    delta needed to reach that target from the current state is computed by
    the caller and supplied via ``parsed['quantity_delta']``.
    """
    return {
        "id": eid,
        "type": _INTENT_TO_TYPE[intent],
        "item": str(parsed["item"]),
        "quantity_delta": _normalize_quantity(parsed["quantity_delta"]),
        "unit": str(parsed.get("unit") or "count"),
        "location": parsed.get("location"),
        "low_threshold": parsed.get("low_threshold"),
        "timestamp": timestamp,
        "source": str(parsed.get("source") or "telegram"),
        "context": message,
        "session_id": session_id,
    }


def _resolve_delta_for_intent(
    *,
    intent: str,
    parsed: Mapping[str, Any],
    current_quantity: float,
) -> float:
    """Compute the signed quantity_delta to persist for this event.

    - ``inventory.add``     -> +parsed['quantity_delta']
    - ``inventory.consume`` -> -parsed['quantity_delta']
    - ``inventory.adjust``  -> parsed['target_quantity'] - current_quantity
    """
    if intent == "inventory.add":
        return _normalize_quantity(parsed.get("quantity_delta", 0))
    if intent == "inventory.consume":
        amount = _normalize_quantity(parsed.get("quantity_delta", 0))
        return -abs(amount)
    if intent == "inventory.adjust":
        if "target_quantity" in parsed:
            target = _normalize_quantity(parsed["target_quantity"])
            return target - current_quantity
        return _normalize_quantity(parsed.get("quantity_delta", 0))
    raise ValueError(f"unsupported write intent {intent!r}")


def _recompute_state(events_path: Path, state_path: Path) -> dict:
    """Walk the full event log and persist a fresh state.yaml.

    State is always derivable from events; replaying from scratch is
    deterministic. This is the load-bearing invariant.
    """
    state: dict[str, dict] = {}
    for row in _iter_events(events_path):
        item = row.get("item")
        if not isinstance(item, str):
            continue
        delta = _normalize_quantity(row.get("quantity_delta", 0))
        unit = str(row.get("unit") or "count")
        location = row.get("location")
        low_threshold = row.get("low_threshold")
        timestamp = row.get("timestamp")

        entry = state.get(item) or {
            "quantity": 0.0,
            "unit": unit,
            "location": location,
            "low_threshold": _DEFAULT_LOW_THRESHOLD,
            "last_seen": timestamp,
            "tags": [],
        }
        # Preserve the latest non-null metadata observed.
        new_quantity = _normalize_quantity(entry.get("quantity", 0)) + delta
        entry = {
            **entry,
            "quantity": new_quantity,
            "unit": unit or entry.get("unit", "count"),
            "location": location if location is not None else entry.get("location"),
            "low_threshold": (
                low_threshold
                if low_threshold is not None
                else entry.get("low_threshold", _DEFAULT_LOW_THRESHOLD)
            ),
            "last_seen": timestamp or entry.get("last_seen"),
        }
        state[item] = entry

    # Coerce numeric ``quantity`` to int when value is exactly integral —
    # makes state.yaml read pleasantly for count-based items without
    # losing precision for fractional units (e.g. "0.5 L").
    for item, entry in state.items():
        q = entry["quantity"]
        if float(q).is_integer():
            entry["quantity"] = int(q)
        # low_threshold: same coercion so the YAML reads cleanly.
        lt = entry.get("low_threshold")
        if isinstance(lt, (int, float)) and float(lt).is_integer():
            entry["low_threshold"] = int(lt)

    atomic_write(state_path, yaml.safe_dump(state, sort_keys=True))
    return state


def _current_quantity(state_path: Path, item: str) -> float:
    """Return the current quantity of ``item`` from state.yaml, or 0 if unknown."""
    if not state_path.exists():
        return 0.0
    try:
        data = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return 0.0
    entry = data.get(item) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return 0.0
    try:
        return _normalize_quantity(entry.get("quantity", 0))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Default LLM-backed extractor + parser (shelled to claude_runner)
# ---------------------------------------------------------------------------


_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_prompt() -> str:
    """Read the inventory prompt; fall back to empty if absent (tests)."""
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _parse_json_payload(text: str) -> dict:
    """Decode an LLM response, tolerating optional ```code-fences```."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [ln for ln in cleaned.splitlines() if not ln.startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _default_extractor(invoker: _ClaudeInvoker) -> _Extractor:
    """Build an extractor backed by the LLM via ``claude_runner.invoke``."""
    system = _load_prompt()

    def _extract(message: str, intent: str) -> dict:
        prompt = (
            f"Intent: {intent}\n"
            "Parse this message into a JSON object describing the inventory event. "
            "Required keys: item (string), and either quantity_delta (number, "
            "always positive — sign is set by the kernel) for add/consume, "
            "or target_quantity (number) for adjust. Optional: unit, location, "
            "low_threshold. Output JSON only.\n\n"
            f"Message: {message}\n"
        )
        response = invoker(prompt, system_prompt=system or None)
        return _parse_json_payload(response.text)

    return _extract


_PARSE_QUERY_HINT = (
    "Parse a free-form inventory question into JSON: "
    '{"mode": "item" | "low_stock" | "list", "item": <string when mode=item>}. '
    "Respond with JSON only — no prose."
)


def _parse_query_via_invoker(invoker: _ClaudeInvoker, query: str) -> dict:
    """Map the user's free-form question onto the query_inventory shape."""
    response = invoker(query, system_prompt=_PARSE_QUERY_HINT)
    return _parse_json_payload(response.text)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def write(
    *,
    intent: str,
    message: str,
    session: Session,
    vault_root: str | os.PathLike[str],
    clock: Optional[Callable[[], datetime]] = None,
    extractor: Optional[_Extractor] = None,
    invoker: Optional[_ClaudeInvoker] = None,
) -> InventoryWriteResult:
    """Persist one inventory event and recompute ``state.yaml``.

    Args:
        intent: must be one of ``inventory.add``, ``inventory.consume``,
            or ``inventory.adjust``.
        message: the user's verbatim natural-language event text.
        session: active session — supplies ``session_id`` for the event row
            and as part of the idempotency key.
        vault_root: vault root on disk.
        clock: pluggable clock (test seam).
        extractor: pluggable parser ``(message, intent) -> dict``; defaults
            to a ``claude_runner``-backed extractor.
        invoker: passed to the default extractor when ``extractor`` is omitted.

    Returns:
        ``InventoryWriteResult`` carrying the event id, paths, and whether
        a new row was actually appended.

    Raises:
        ValueError: ``intent`` is not a registered write intent.
    """
    if intent not in _WRITE_INTENTS:
        raise ValueError(
            f"inventory.write only handles {_WRITE_INTENTS}, not {intent!r}"
        )
    if not message or not message.strip():
        raise ValueError("inventory write requires a non-empty message")

    extract = extractor or _default_extractor(invoker or claude_invoke)
    parsed = dict(extract(message, intent) or {})
    if "item" not in parsed or not str(parsed["item"]).strip():
        raise ValueError("inventory extractor must return an 'item' field")

    events_path = Path(vault_root) / _EVENTS_RELATIVE
    state_path = Path(vault_root) / _STATE_RELATIVE

    # Resolve the signed delta for this intent (adjust needs current state).
    current_q = _current_quantity(state_path, str(parsed["item"]))
    delta = _resolve_delta_for_intent(
        intent=intent, parsed=parsed, current_quantity=current_q
    )
    parsed["quantity_delta"] = delta

    eid = event_id(intent=intent, message=message, session_id=session.session_id)
    seen = _existing_ids(events_path)

    timestamp = _now(clock).isoformat()
    event_row = _build_event(
        parsed=parsed,
        intent=intent,
        message=message,
        session_id=session.session_id,
        timestamp=timestamp,
        eid=eid,
    )

    appended = False
    if eid not in seen:
        _append_event(events_path, event_row)
        appended = True

    # State.yaml is always recomputed — even on a no-op append we want to
    # be sure the on-disk state is consistent with the event log (e.g. if
    # an earlier crash left state.yaml stale).
    _recompute_state(events_path, state_path)

    return InventoryWriteResult(
        intent=intent,
        path=events_path,
        state_path=state_path,
        event_id=eid,
        appended=appended,
        item=str(parsed["item"]),
        quantity_delta=delta,
    )


# ---------------------------------------------------------------------------
# query_inventory — pure-Python state lookup
# ---------------------------------------------------------------------------


def _load_state(vault_root: str | os.PathLike[str]) -> dict:
    """Load state.yaml as a dict; return ``{}`` when missing/malformed."""
    state_path = Path(vault_root) / _STATE_RELATIVE
    if not state_path.exists():
        return {}
    try:
        data = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _state_row(state: Mapping[str, Any], item: str) -> Optional[dict]:
    """Case-insensitive lookup of ``item`` in state."""
    if item in state and isinstance(state[item], Mapping):
        return dict(state[item])
    target = item.lower().strip()
    for key, value in state.items():
        if isinstance(key, str) and key.lower().strip() == target:
            if isinstance(value, Mapping):
                return dict(value)
    return None


def query_inventory(
    *,
    mode: str,
    item: Optional[str] = None,
    vault_root: str | os.PathLike[str],
) -> dict:
    """Look up state for a single item, low-stock items, or the full list.

    Args:
        mode: ``item`` | ``low_stock`` | ``list``.
        item: the item name (required when ``mode='item'``).
        vault_root: vault root on disk; missing state is non-fatal.

    Returns:
        A dict shaped per mode. ``mode='item'`` returns the canonical state
        row plus ``found``. ``mode='low_stock'`` and ``mode='list'`` return
        ``items`` (list of state rows with ``item`` keyed in).

    Raises:
        ValueError: ``mode`` is not one of the supported values.
    """
    if mode not in ("item", "low_stock", "list"):
        raise ValueError(
            f"query_inventory mode must be item|low_stock|list, not {mode!r}"
        )

    state = _load_state(vault_root)

    if mode == "item":
        if not item:
            raise ValueError("query_inventory mode='item' requires an item argument")
        row = _state_row(state, item)
        if row is None:
            return {
                "mode": "item",
                "item": item,
                "found": False,
                "quantity": 0,
                "unit": "count",
                "location": None,
                "low_threshold": _DEFAULT_LOW_THRESHOLD,
            }
        return {
            "mode": "item",
            "item": item,
            "found": True,
            "quantity": row.get("quantity", 0),
            "unit": row.get("unit", "count"),
            "location": row.get("location"),
            "low_threshold": row.get("low_threshold", _DEFAULT_LOW_THRESHOLD),
        }

    items: list[dict] = []
    for name, row in state.items():
        if not isinstance(name, str) or not isinstance(row, Mapping):
            continue
        quantity = row.get("quantity", 0)
        threshold = row.get("low_threshold", _DEFAULT_LOW_THRESHOLD)
        if mode == "low_stock":
            try:
                if _normalize_quantity(quantity) >= _normalize_quantity(threshold):
                    continue
            except (TypeError, ValueError):
                continue
        items.append(
            {
                "item": name,
                "quantity": quantity,
                "unit": row.get("unit", "count"),
                "location": row.get("location"),
                "low_threshold": threshold,
            }
        )

    return {"mode": mode, "items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# read — natural-language query -> structured query -> reply
# ---------------------------------------------------------------------------


def _coerce_parsed_query(parsed: Mapping[str, Any], intent: str) -> dict:
    """Coerce parser output into the kwargs ``query_inventory`` expects."""
    if intent == "inventory.list_low":
        return {"mode": "low_stock", "item": None}
    raw_mode = str(parsed.get("mode") or "").strip().lower()
    item = str(parsed.get("item") or "").strip()
    if raw_mode in ("item", "low_stock", "list"):
        mode = raw_mode
    elif item:
        mode = "item"
    else:
        mode = "list"
    return {"mode": mode, "item": item or None}


def _build_reply(
    *,
    mode: str,
    item: Optional[str],
    aggregated: Mapping[str, Any],
) -> str:
    """Produce a one-sentence answer the user can sanity-check against state.yaml."""
    if mode == "item":
        target = item or aggregated.get("item") or "that item"
        if not aggregated.get("found"):
            return f"You don't have any {target} on file."
        qty = aggregated.get("quantity", 0)
        unit = aggregated.get("unit") or "count"
        location = aggregated.get("location")
        location_clause = f" in the {location}" if location else ""
        unit_clause = "" if unit == "count" else f" {unit}"
        return f"You have {qty}{unit_clause} of {target}{location_clause}."

    items = aggregated.get("items") or []
    if mode == "low_stock":
        if not items:
            return "Nothing is running low right now."
        names = ", ".join(sorted(str(i["item"]) for i in items))
        return f"Running low: {names}."
    # mode == 'list'
    if not items:
        return "Inventory is empty."
    names = ", ".join(sorted(str(i["item"]) for i in items))
    return f"Inventory: {names}."


def read(
    *,
    intent: str,
    query: str,
    vault_root: str | os.PathLike[str],
    query_parser: Optional[Callable[[str], Mapping[str, Any]]] = None,
    invoker: Optional[_ClaudeInvoker] = None,
) -> InventoryReadResult:
    """Answer ``inventory.query`` or ``inventory.list_low``.

    Args:
        intent: ``inventory.query`` or ``inventory.list_low``.
        query: the user's free-form question.
        vault_root: vault root on disk.
        query_parser: pluggable mapper from query text to a dict with
            ``mode`` and (optionally) ``item``.
        invoker: pluggable ``claude_runner.invoke`` used when no parser
            is supplied.

    Returns:
        ``InventoryReadResult`` with the rendered reply and structured
        fields the kernel audit-logs.

    Raises:
        ValueError: ``intent`` is not a registered read intent.
    """
    if intent not in _READ_INTENTS:
        raise ValueError(
            f"inventory.read only handles {_READ_INTENTS}, not {intent!r}"
        )

    if intent == "inventory.list_low":
        parsed: Mapping[str, Any] = {"mode": "low_stock"}
    elif query_parser is not None:
        parsed = query_parser(query) or {}
    else:
        parsed = _parse_query_via_invoker(invoker or claude_invoke, query)

    coerced = _coerce_parsed_query(parsed, intent)
    mode = coerced["mode"]
    item = coerced["item"]

    if mode == "item":
        aggregated = query_inventory(mode="item", item=item, vault_root=vault_root)
        count = float(_normalize_quantity(aggregated.get("quantity", 0)))
    else:
        aggregated = query_inventory(mode=mode, vault_root=vault_root)
        count = float(aggregated.get("count", 0))

    reply = _build_reply(mode=mode, item=item, aggregated=aggregated)

    return InventoryReadResult(
        intent=intent,
        reply_text=reply,
        mode=mode,
        item=item or "",
        count=count,
    )
