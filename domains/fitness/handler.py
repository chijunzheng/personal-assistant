"""Fitness plugin — write (workouts/meals/metrics/profile) + read (query_fitness).

The plugin lives behind a small surface. Issue #7 covers the *logging*
side: workouts, meals, metrics, and profile updates. Plan generation
(``fitness.workout_plan`` / ``fitness.nutrition_plan``) is issue #8 — not
implemented here.

  - ``write(intent, message, session, ...)`` -> ``FitnessWriteResult``.
    Routes by intent:
      - ``fitness.workout_log``    -> append to ``workouts.jsonl``.
      - ``fitness.meal_log``       -> append to ``meals.jsonl`` (+ optional
        photo under ``meal_photos/{date}-{id}.jpg``).
      - ``fitness.metric_log``     -> append to ``metrics.jsonl``; if
        ``kind == 'weight'`` also rewrite ``profile.yaml:weight_kg`` and
        log a ``profile_event`` row.
      - ``fitness.profile_update`` -> append to ``profile_events.jsonl`` and
        atomically rewrite ``profile.yaml``. If a macro-relevant field
        changes, recompute target_calories_kcal + macros via Mifflin-St
        Jeor + activity multiplier and log those as additional events.

  - ``read(intent, query, ...)`` -> reply text for ``fitness.query``.
  - ``query_fitness(kind, ...)`` -> dict (re-exported from ``_query``).

Plugin discipline:
  - Idempotent on a content-derived sha256 ``id`` per row.
  - Log-silent — never writes the audit log; the kernel does that.
  - All vault writes route through ``kernel.vault.atomic_write`` so a
    Drive sync mid-update never observes a half-written file.
  - First-run bootstrap: copies ``profile.template.yaml`` to
    ``vault/fitness/profile.yaml`` if the latter does not exist.

The non-trivial helpers live in sibling modules to keep this file as a
thin dispatch layer:
  - ``domains.fitness._io``     — JSONL/YAML I/O, sha256, bootstrap.
  - ``domains.fitness._macros`` — profile field edits + Mifflin-St Jeor.
  - ``domains.fitness._query``  — pure-Python aggregation surface.
  - ``domains.fitness._paths``  — path constants.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from kernel.claude_runner import ClaudeResponse, invoke as claude_invoke
from kernel.session import Session

from domains.fitness._io import (
    append_jsonl,
    ensure_profile_bootstrapped,
    existing_ids,
    load_aliases,
    now,
    sha256_parts,
    store_binary_atomically,
)
from domains.fitness._macros import update_profile_field
from domains.fitness._paths import (
    MEAL_PHOTOS_RELATIVE,
    MEALS_RELATIVE,
    METRICS_RELATIVE,
    PROFILE_RELATIVE,
    WORKOUTS_RELATIVE,
)
from domains.fitness._query import query_fitness

__all__ = [
    "FitnessWriteResult",
    "query_fitness",
    "read",
    "write",
]


# ---------------------------------------------------------------------------
# Constants — intent labels
# ---------------------------------------------------------------------------


_INTENT_WORKOUT = "fitness.workout_log"
_INTENT_MEAL = "fitness.meal_log"
_INTENT_METRIC = "fitness.metric_log"
_INTENT_PROFILE_UPDATE = "fitness.profile_update"
_INTENT_QUERY = "fitness.query"

_WRITE_INTENTS = (
    _INTENT_WORKOUT,
    _INTENT_MEAL,
    _INTENT_METRIC,
    _INTENT_PROFILE_UPDATE,
)


# ---------------------------------------------------------------------------
# Public dataclasses + protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FitnessWriteResult:
    """Return value of ``write`` — what the kernel needs to audit-log + reply.

    Attributes:
        intent: the registered intent label.
        path: the canonical JSONL or YAML file the row landed in.
        row_id: sha256 id of the row (whether or not appended).
        appended: ``True`` if a fresh row was added; ``False`` if no-op.
        extra: small dict of intent-specific extras (e.g. macro updates).
    """

    intent: str
    path: Path
    row_id: str
    appended: bool
    extra: Mapping[str, Any]


class _Extractor(Protocol):
    """Maps (message, intent) -> a parsed dict.

    The shape of the dict depends on the intent — see each ``_handle_*``
    helper below for the exact contract. Tests inject deterministic
    extractors so the LLM is not actually called.
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
# Default LLM-backed extractor (production fallback)
# ---------------------------------------------------------------------------


_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_prompt() -> str:
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
            "Parse this message into a JSON object matching the field shape "
            "the fitness handler expects for this intent. Output JSON only.\n\n"
            f"Message: {message}\n"
        )
        response = invoker(prompt, system_prompt=system or None)
        return _parse_json_payload(response.text)

    return _extract


# ---------------------------------------------------------------------------
# Workout log
# ---------------------------------------------------------------------------


def _normalize_exercise_name(name: str, aliases: Mapping[str, str]) -> str:
    """Map a user-typed exercise name through the alias table.

    Match is case-insensitive on the trimmed key. If no alias hits, the
    original (trimmed) name is returned so the row still has a reasonable
    canonical form.
    """
    raw = str(name).strip()
    canonical = aliases.get(raw.lower())
    if canonical:
        return canonical
    return raw


def _normalize_exercises(
    exercises: Iterable[Mapping[str, Any]],
    *,
    aliases: Mapping[str, str],
) -> list[dict]:
    """Apply alias normalization + fill missing fields with ``None``."""
    out: list[dict] = []
    for ex in exercises or []:
        if not isinstance(ex, Mapping):
            continue
        out.append(
            {
                "name": _normalize_exercise_name(ex.get("name", ""), aliases),
                "sets": ex.get("sets"),
                "reps": ex.get("reps"),
                "weight_kg": ex.get("weight_kg"),
                "distance_km": ex.get("distance_km"),
                "duration_min": ex.get("duration_min"),
                "notes": ex.get("notes"),
            }
        )
    return out


def _workout_id(*, date_iso: str, exercises_normalized: list[dict], notes: str) -> str:
    """sha256 over date | normalized-exercises-json | notes."""
    serialized = json.dumps(exercises_normalized, sort_keys=True, default=str)
    return sha256_parts([date_iso, serialized, notes or ""])


def _handle_workout_log(
    *,
    parsed: Mapping[str, Any],
    message: str,
    session: Session,
    vault_root: Path,
    timestamp: datetime,
    aliases: Mapping[str, str],
) -> FitnessWriteResult:
    """Append one workout row to ``workouts.jsonl`` (idempotent on id)."""
    date_iso = timestamp.date().isoformat()
    exercises = _normalize_exercises(parsed.get("exercises") or [], aliases=aliases)
    notes = str(parsed.get("session_notes") or "")
    row_id = _workout_id(date_iso=date_iso, exercises_normalized=exercises, notes=notes)

    workouts_path = vault_root / WORKOUTS_RELATIVE
    seen = existing_ids(workouts_path)

    row = {
        "id": row_id,
        "date": date_iso,
        "type": str(parsed.get("type") or "mixed"),
        "duration_min": parsed.get("duration_min"),
        "intensity": parsed.get("intensity"),
        "exercises": exercises,
        "rpe": parsed.get("rpe"),
        "session_notes": notes or None,
        "plan_id": parsed.get("plan_id"),
        "source": str(parsed.get("source") or "telegram"),
        "tags": list(parsed.get("tags") or []),
        "context": message,
        "session_id": session.session_id,
    }

    appended = False
    if row_id not in seen:
        append_jsonl(workouts_path, row)
        appended = True

    return FitnessWriteResult(
        intent=_INTENT_WORKOUT,
        path=workouts_path,
        row_id=row_id,
        appended=appended,
        extra={"exercise_count": len(exercises)},
    )


# ---------------------------------------------------------------------------
# Meal log
# ---------------------------------------------------------------------------


def _normalize_meal_items(items: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Fill missing macro fields with ``0`` and confidence with ``0.5``."""
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        out.append(
            {
                "name": str(item.get("name", "")).strip(),
                "quantity": float(item.get("quantity") or 0),
                "unit": str(item.get("unit") or "count"),
                "calories_kcal": float(item.get("calories_kcal") or 0),
                "protein_g": float(item.get("protein_g") or 0),
                "carbs_g": float(item.get("carbs_g") or 0),
                "fat_g": float(item.get("fat_g") or 0),
                "confidence": float(item.get("confidence") or 0.5),
            }
        )
    return out


def _meal_totals(items: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Sum macro fields across the items list. Integral floats become ints."""
    totals = {
        "total_kcal": 0.0,
        "total_protein_g": 0.0,
        "total_carbs_g": 0.0,
        "total_fat_g": 0.0,
    }
    for item in items:
        totals["total_kcal"] += float(item.get("calories_kcal") or 0)
        totals["total_protein_g"] += float(item.get("protein_g") or 0)
        totals["total_carbs_g"] += float(item.get("carbs_g") or 0)
        totals["total_fat_g"] += float(item.get("fat_g") or 0)
    return {k: (int(v) if float(v).is_integer() else v) for k, v in totals.items()}


def _meal_id(*, ts_iso: str, items_normalized: list[dict]) -> str:
    serialized = json.dumps(items_normalized, sort_keys=True, default=str)
    return sha256_parts([ts_iso, serialized])


def _store_meal_photo(
    *,
    photo_bytes: bytes,
    vault_root: Path,
    date_iso: str,
    row_id: str,
) -> Path:
    """Persist photo bytes under ``meal_photos/{date}-{id}.jpg``."""
    photos_dir = vault_root / MEAL_PHOTOS_RELATIVE
    path = photos_dir / f"{date_iso}-{row_id}.jpg"
    store_binary_atomically(path=path, payload=photo_bytes)
    return path


def _handle_meal_log(
    *,
    parsed: Mapping[str, Any],
    message: str,
    session: Session,
    vault_root: Path,
    timestamp: datetime,
    photo_bytes: Optional[bytes],
) -> FitnessWriteResult:
    """Append one meal row + optionally store a photo."""
    ts_iso = timestamp.isoformat()
    items = _normalize_meal_items(parsed.get("items") or [])
    totals = _meal_totals(items)
    row_id = _meal_id(ts_iso=ts_iso, items_normalized=items)

    meals_path = vault_root / MEALS_RELATIVE
    seen = existing_ids(meals_path)

    photo_path: Optional[Path] = None
    if photo_bytes:
        photo_path = _store_meal_photo(
            photo_bytes=photo_bytes,
            vault_root=vault_root,
            date_iso=timestamp.date().isoformat(),
            row_id=row_id,
        )

    row = {
        "id": row_id,
        "ts": ts_iso,
        "meal_type": str(parsed.get("meal_type") or "snack"),
        "items": items,
        **totals,
        "photo_path": str(photo_path) if photo_path else None,
        "source": str(parsed.get("source") or "telegram"),
        "notes": parsed.get("notes"),
        "context": message,
        "session_id": session.session_id,
    }

    appended = False
    if row_id not in seen:
        append_jsonl(meals_path, row)
        appended = True

    return FitnessWriteResult(
        intent=_INTENT_MEAL,
        path=meals_path,
        row_id=row_id,
        appended=appended,
        extra={
            "totals": totals,
            "photo_path": str(photo_path) if photo_path else None,
        },
    )


# ---------------------------------------------------------------------------
# Metric log
# ---------------------------------------------------------------------------


def _metric_id(*, ts_iso: str, kind: str, value: float) -> str:
    return sha256_parts([ts_iso, kind, str(value)])


def _handle_metric_log(
    *,
    parsed: Mapping[str, Any],
    message: str,
    session: Session,
    vault_root: Path,
    timestamp: datetime,
) -> FitnessWriteResult:
    """Append one metric row + (for kind=weight) update profile.yaml."""
    ts_iso = timestamp.isoformat()
    kind = str(parsed.get("kind") or "").strip()
    if not kind:
        raise ValueError("metric_log extractor must return a 'kind' field")
    try:
        value = float(parsed["value"])
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError(
            "metric_log extractor must return a numeric 'value'"
        ) from err
    unit = str(parsed.get("unit") or "")

    row_id = _metric_id(ts_iso=ts_iso, kind=kind, value=value)

    metrics_path = vault_root / METRICS_RELATIVE
    seen = existing_ids(metrics_path)

    row = {
        "id": row_id,
        "ts": ts_iso,
        "kind": kind,
        "value": value,
        "unit": unit,
        "source": str(parsed.get("source") or "telegram"),
        "notes": parsed.get("notes"),
        "context": message,
        "session_id": session.session_id,
    }

    appended = False
    if row_id not in seen:
        append_jsonl(metrics_path, row)
        appended = True

    extra: dict[str, Any] = {"kind": kind, "value": value}

    # Weight metrics also rewrite profile.yaml (and log a profile_event).
    if kind == "weight" and appended:
        update_profile_field(
            field="weight_kg",
            new_value=value,
            vault_root=vault_root,
            timestamp=timestamp,
            source="telegram",
            recompute_macros=False,
        )
        extra["profile_updated"] = True

    return FitnessWriteResult(
        intent=_INTENT_METRIC,
        path=metrics_path,
        row_id=row_id,
        appended=appended,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Profile update
# ---------------------------------------------------------------------------


def _handle_profile_update(
    *,
    parsed: Mapping[str, Any],
    message: str,  # noqa: ARG001 — kept symmetric with the other handlers
    session: Session,  # noqa: ARG001 — kept symmetric with the other handlers
    vault_root: Path,
    timestamp: datetime,
) -> FitnessWriteResult:
    """Append a profile_event + atomically rewrite profile.yaml + maybe recompute macros."""
    field = str(parsed.get("field") or "").strip()
    if not field:
        raise ValueError("profile_update extractor must return a 'field' name")
    if "new_value" not in parsed:
        raise ValueError("profile_update extractor must return a 'new_value'")
    new_value = parsed["new_value"]

    result = update_profile_field(
        field=field,
        new_value=new_value,
        vault_root=vault_root,
        timestamp=timestamp,
        source="telegram",
        recompute_macros=True,
    )

    return FitnessWriteResult(
        intent=_INTENT_PROFILE_UPDATE,
        path=vault_root / PROFILE_RELATIVE,
        row_id=result["event_id"],
        appended=True,
        extra={
            "field": field,
            "old_value": result["old_value"],
            "new_value": result["new_value"],
            "macro_event_ids": tuple(result["macro_event_ids"]),
        },
    )


# ---------------------------------------------------------------------------
# Public entry point: write
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
    photo_bytes: Optional[bytes] = None,
) -> FitnessWriteResult:
    """Persist one fitness event and (optionally) update profile.yaml.

    Args:
        intent: one of ``fitness.workout_log``, ``fitness.meal_log``,
            ``fitness.metric_log``, ``fitness.profile_update``.
        message: the user's verbatim natural-language event text.
        session: active session — supplies ``session_id`` for the row.
        vault_root: vault root on disk.
        clock: pluggable clock (test seam).
        extractor: pluggable parser ``(message, intent) -> dict``; defaults
            to a ``claude_runner``-backed extractor for production callers.
        invoker: passed to the default extractor when ``extractor`` is omitted.
        photo_bytes: bytes of an attached photo (only honored for
            ``fitness.meal_log``); when present, the photo is stored under
            ``vault/fitness/meal_photos/{date}-{id}.jpg``.

    Returns:
        ``FitnessWriteResult`` carrying the row id, path, and appended flag.

    Raises:
        ValueError: ``intent`` is not a registered write intent, or the
            extractor produced an unusable payload.
    """
    if intent not in _WRITE_INTENTS:
        raise ValueError(
            f"fitness.write only handles {_WRITE_INTENTS}, not {intent!r}"
        )
    if not message or not message.strip():
        raise ValueError("fitness write requires a non-empty message")

    vault = Path(vault_root)
    ensure_profile_bootstrapped(vault)

    extract = extractor or _default_extractor(invoker or claude_invoke)
    parsed = dict(extract(message, intent) or {})

    timestamp = now(clock)

    if intent == _INTENT_WORKOUT:
        aliases = load_aliases(vault)
        return _handle_workout_log(
            parsed=parsed,
            message=message,
            session=session,
            vault_root=vault,
            timestamp=timestamp,
            aliases=aliases,
        )
    if intent == _INTENT_MEAL:
        return _handle_meal_log(
            parsed=parsed,
            message=message,
            session=session,
            vault_root=vault,
            timestamp=timestamp,
            photo_bytes=photo_bytes,
        )
    if intent == _INTENT_METRIC:
        return _handle_metric_log(
            parsed=parsed,
            message=message,
            session=session,
            vault_root=vault,
            timestamp=timestamp,
        )
    # _INTENT_PROFILE_UPDATE
    return _handle_profile_update(
        parsed=parsed,
        message=message,
        session=session,
        vault_root=vault,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# read — natural-language fitness.query -> query_fitness -> reply
# ---------------------------------------------------------------------------


_PARSE_QUERY_HINT = (
    "Parse a free-form fitness question into JSON: "
    '{"kind": "workouts" | "meals" | "metrics" | "profile" | "plans", '
    '"date_range": [<start_iso>, <end_iso>], '
    '"agg": "count" | "list" | "sum" | "avg" | "trend" | "volume" | "last", '
    '"metric_kind": <string for kind=metrics>}. '
    "Respond with JSON only — no prose."
)


def _parse_query_via_invoker(invoker: _ClaudeInvoker, query: str) -> dict:
    response = invoker(query, system_prompt=_PARSE_QUERY_HINT)
    return _parse_json_payload(response.text)


def _coerce_parsed_query(parsed: Mapping[str, Any]) -> dict:
    """Coerce parser output into kwargs for ``query_fitness``."""
    kind = str(parsed.get("kind") or "workouts").strip().lower()
    raw_range = parsed.get("date_range")
    if isinstance(raw_range, (list, tuple)) and len(raw_range) >= 2:
        date_range: Optional[tuple[str, str]] = (
            str(raw_range[0]),
            str(raw_range[1]),
        )
    else:
        date_range = None
    agg = parsed.get("agg")
    return {
        "kind": kind,
        "date_range": date_range,
        "agg": str(agg) if agg else None,
        "metric_kind": parsed.get("metric_kind"),
        "workout_type": parsed.get("workout_type"),
        "meal_type": parsed.get("meal_type"),
    }


def _format_workouts_reply(result: Mapping[str, Any]) -> str:
    """Render a workout-query result as a one-liner with a per-week cadence hint."""
    count = int(result.get("count") or 0)
    date_range = result.get("date_range") or (None, None)
    start, end = (
        date_range
        if isinstance(date_range, (list, tuple))
        else (None, None)
    )
    if start and end:
        try:
            span_days = (
                datetime.fromisoformat(str(end)[:10] + "T00:00:00").date()
                - datetime.fromisoformat(str(start)[:10] + "T00:00:00").date()
            ).days + 1
        except (TypeError, ValueError):
            span_days = 0
        weeks = max(span_days / 7, 1) if span_days else 0
        per_week = (count / weeks) if weeks else 0.0
        if span_days:
            return (
                f"{count} sessions between {str(start)[:10]} and {str(end)[:10]} "
                f"(avg {per_week:.1f}/wk)."
            )
    return f"{count} sessions on file."


def _format_metrics_reply(result: Mapping[str, Any]) -> str:
    avg = float(result.get("avg") or 0)
    trend = float(result.get("trend") or 0)
    direction = (
        "trending down"
        if trend < 0
        else ("trending up" if trend > 0 else "flat")
    )
    return f"Avg {avg:.1f}, {direction} ({trend:+.2f}/sample)."


def _format_meals_reply(result: Mapping[str, Any]) -> str:
    if result.get("agg") == "avg":
        return (
            f"Avg {float(result.get('avg_kcal') or 0):.0f} kcal / "
            f"{float(result.get('avg_protein_g') or 0):.0f}g protein "
            f"across {int(result.get('count') or 0)} meals."
        )
    return (
        f"{int(result.get('count') or 0)} meals: "
        f"{int(result.get('total_kcal') or 0)} kcal, "
        f"{int(result.get('total_protein_g') or 0)}g protein."
    )


def _build_query_reply(result: Mapping[str, Any]) -> str:
    kind = result.get("kind")
    if kind == "workouts":
        return _format_workouts_reply(result)
    if kind == "metrics":
        return _format_metrics_reply(result)
    if kind == "meals":
        return _format_meals_reply(result)
    if kind == "profile":
        goal = result.get("goal", "n/a")
        weight = result.get("weight_kg", "n/a")
        return f"Profile: goal={goal}, weight_kg={weight}."
    if kind == "plans":
        return f"{int(result.get('count') or 0)} plans on file."
    return "Result n/a."


def read(
    *,
    intent: str,
    query: str,
    vault_root: str | os.PathLike[str],
    query_parser: Optional[Callable[[str], Mapping[str, Any]]] = None,
    invoker: Optional[_ClaudeInvoker] = None,
) -> str:
    """Answer a ``fitness.query`` with a real numeric aggregation.

    Args:
        intent: must be ``fitness.query``.
        query: the user's free-form question.
        vault_root: vault root on disk.
        query_parser: pluggable mapper from the question to a dict with
            ``kind`` + ``date_range`` + ``agg`` (and optional ``metric_kind``).
            Tests inject one so the unit test doesn't shell to the LLM.
        invoker: pluggable ``claude_runner.invoke`` used when no parser is
            supplied.

    Returns:
        A reply string the orchestrator hands back to Telegram.

    Raises:
        ValueError: ``intent`` is not ``fitness.query``.
    """
    if intent != _INTENT_QUERY:
        raise ValueError(
            f"fitness.read only handles {_INTENT_QUERY}, not {intent!r}"
        )

    if query_parser is not None:
        parsed = query_parser(query) or {}
    else:
        parsed = _parse_query_via_invoker(invoker or claude_invoke, query)

    coerced = _coerce_parsed_query(parsed)
    aggregated = query_fitness(vault_root=vault_root, **coerced)
    return _build_query_reply(aggregated)
