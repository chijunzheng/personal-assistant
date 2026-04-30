"""Head-to-head eval harness — runs every case under both configs.

Walks ``domains/*/eval/cases.jsonl`` AND ``eval/cases/*.jsonl``, dedupes
cases by ``id``, and runs each case under ``configs/default.yaml`` and
``configs/baseline.yaml``. Per case + config it captures:

  * the reply text
  * the audit JSONL the case produced (path)
  * tokens in / out / total
  * the tool-call sequence (lifted from the audit)
  * duration_ms
  * status: ``ok`` | ``error``

Results land at ``eval/results/<config>-<timestamp>.json``.

Design notes:

  1. The harness **never** calls ``claude_runner`` directly. It receives an
     ``invoke_case`` callable that runs one (case, config) pair and returns
     a dict with reply / token / tool-call data. Production wires this to a
     real per-domain runner; tests inject a deterministic stub. This keeps
     the harness test-friendly without mocking the whole LLM stack.

  2. ``vault_setup`` materialization is best-effort but covers the four
     fixture shapes used in ``domains/fitness/eval/cases.jsonl``:
       - mapping like ``{"profile.yaml": {...}}`` -> YAML dump under the
         domain directory
       - ``{"<file>.jsonl_recent": [...]}`` -> JSONL append, with relative
         dates ``T-1``/``T-2``/... resolved against ``today``
       - ``{"journal/T-1.md": "<body>"}`` -> markdown file with resolved date
       - ``{"<sub-path>/<file>": <data>}`` -> dump as YAML if dict, JSONL if
         list, raw text otherwise

  3. Each case runs in an isolated temp vault — this is the precondition
     for issue #14 (journal cases), which exercises subjective-context
     cross-domain retrieval and needs a clean fixture vault every time.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

__all__ = [
    "discover_cases",
    "materialize_vault_setup",
    "run_cases",
    "main",
]


# ---------------------------------------------------------------------------
# Case discovery
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Return a list of dicts from a JSONL file (one object per non-empty line)."""
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError:
                # Skip malformed lines silently — eval cases are human-curated
                # but we don't want one bad row to abort the whole sweep.
                continue
    return rows


def discover_cases(
    *,
    project_root: str | os.PathLike[str],
) -> list[dict[str, Any]]:
    """Find and dedupe cases from per-domain + top-level locations.

    Returns a sorted list of case dicts. Each case has ``_source`` set to
    the relative path of the JSONL file it came from. Duplicates by
    ``id`` keep the first occurrence (per-domain wins because that walk
    happens first in deterministic order).
    """
    root = Path(project_root)
    found: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Per-domain cases — walk in sorted order for deterministic discovery.
    domains_dir = root / "domains"
    if domains_dir.exists():
        for domain_dir in sorted(domains_dir.iterdir()):
            if not domain_dir.is_dir():
                continue
            cases_path = domain_dir / "eval" / "cases.jsonl"
            if not cases_path.exists():
                continue
            try:
                rel = str(cases_path.relative_to(root))
            except ValueError:
                rel = str(cases_path)
            for case in _read_jsonl(cases_path):
                cid = case.get("id")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                found.append({**case, "_source": rel})

    # Top-level synthetic cases under ``eval/cases/*.jsonl``.
    synth_dir = root / "eval" / "cases"
    if synth_dir.exists():
        for jsonl in sorted(synth_dir.glob("*.jsonl")):
            try:
                rel = str(jsonl.relative_to(root))
            except ValueError:
                rel = str(jsonl)
            for case in _read_jsonl(jsonl):
                cid = case.get("id")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                found.append({**case, "_source": rel})

    return found


# ---------------------------------------------------------------------------
# Vault setup materialization
# ---------------------------------------------------------------------------


_RELATIVE_DATE_PREFIX = "T-"


def _resolve_relative_date(token: str, *, today: date) -> str:
    """Resolve ``T-1`` / ``T-3`` to an ISO date string anchored at ``today``."""
    if not isinstance(token, str) or not token.startswith(_RELATIVE_DATE_PREFIX):
        return token
    suffix = token[len(_RELATIVE_DATE_PREFIX):]
    try:
        offset = int(suffix)
    except ValueError:
        return token
    return (today - timedelta(days=offset)).isoformat()


def _is_jsonl_recent_key(key: str) -> bool:
    """``workouts.jsonl_recent``, ``meals.jsonl_recent``, etc."""
    return key.endswith(".jsonl_recent")


def _strip_recent_suffix(key: str) -> str:
    """Drop ``_recent`` so the on-disk file is ``workouts.jsonl``."""
    return key[:-len("_recent")] if key.endswith("_recent") else key


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` (creating parents) — eval-side, not vault.atomic_write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _dump_yaml(data: Any) -> str:
    """Serialize a Python object as YAML (sort keys for determinism)."""
    return yaml.safe_dump(data, sort_keys=True)


def _dump_jsonl(rows: list[Any]) -> str:
    """Serialize a list of rows as JSONL."""
    return "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n"


def materialize_vault_setup(
    spec: dict[str, Any],
    *,
    vault_root: str | os.PathLike[str],
    domain: str,
    today: date | None = None,
) -> None:
    """Materialize a ``vault_setup`` fixture into the on-disk temp vault.

    Supported shapes in ``spec`` (keyed off the patterns in the existing
    fitness eval cases.jsonl):

      * ``"profile.yaml"`` → YAML at ``vault/<domain>/profile.yaml``
      * ``"workouts.jsonl_recent"`` → JSONL rows at ``vault/<domain>/workouts.jsonl``;
        any ``"date": "T-N"`` is resolved to ``today - N days``
      * ``"journal/T-1.md"`` → markdown at ``vault/journal/<resolved>.md``
      * ``"inventory/state.yaml"`` → YAML at the explicit nested path
      * Any path containing ``/`` is taken verbatim under ``vault_root``;
        otherwise it is placed under ``vault/<domain>/``.
    """
    vault = Path(vault_root)
    today = today or date.today()

    for key, value in (spec or {}).items():
        if _is_jsonl_recent_key(key):
            target_name = _strip_recent_suffix(key)
            target = vault / domain / target_name
            resolved_rows = []
            for row in value or []:
                if isinstance(row, dict) and "date" in row:
                    new_row = dict(row)
                    new_row["date"] = _resolve_relative_date(row["date"], today=today)
                    resolved_rows.append(new_row)
                else:
                    resolved_rows.append(row)
            _atomic_write_text(target, _dump_jsonl(resolved_rows))
            continue

        # Resolve relative-date markers in the key (e.g., ``journal/T-1.md``).
        resolved_key = _resolve_key_relative_dates(key, today=today)

        # Normalize relative path under vault_root: paths with a ``/`` are taken
        # as-is; bare filenames go under ``vault/<domain>/``.
        if "/" in resolved_key:
            target = vault / resolved_key
        else:
            target = vault / domain / resolved_key

        if isinstance(value, dict):
            _atomic_write_text(target, _dump_yaml(value))
        elif isinstance(value, list):
            _atomic_write_text(target, _dump_jsonl(value))
        else:
            _atomic_write_text(target, str(value))


def _resolve_key_relative_dates(key: str, *, today: date) -> str:
    """Resolve any ``T-N`` token in a path-like key.

    Example: ``journal/T-1.md`` -> ``journal/2026-04-28.md``.
    """
    parts = key.split("/")
    resolved_parts: list[str] = []
    for part in parts:
        # Try the whole part (handles ``T-1.md`` by splitting the suffix).
        stem, dot, ext = part.partition(".")
        if stem.startswith(_RELATIVE_DATE_PREFIX):
            new_stem = _resolve_relative_date(stem, today=today)
            resolved_parts.append(new_stem + (dot + ext if dot else ""))
        else:
            resolved_parts.append(part)
    return "/".join(resolved_parts)


# ---------------------------------------------------------------------------
# Per-case execution
# ---------------------------------------------------------------------------


CONFIGS = ("default", "baseline")


def _now_timestamp() -> str:
    """UTC timestamp in compact form for results filenames."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")


def _write_audit_lines(path: Path, lines: Iterable[dict[str, Any]]) -> None:
    """Persist audit lines as JSONL — used to record what the stub invoker emitted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, sort_keys=True, default=str) + "\n")


def _run_one(
    case: dict[str, Any],
    *,
    config_label: str,
    invoke_case: Callable[..., dict[str, Any]],
    project_root: Path,
    audit_dir: Path,
) -> dict[str, Any]:
    """Run a single (case, config) pair in an isolated temp vault.

    Returns a result row matching the issue-#13 schema.
    """
    case_id = case.get("id", "<no-id>")
    audit_path = audit_dir / f"{case_id}-{config_label}.jsonl"

    started = time.monotonic()
    status = "ok"
    error: str | None = None
    reply = ""
    tokens_in = 0
    tokens_out = 0
    tool_calls: list[str] = []
    invoker_duration_ms = 0
    audit_lines: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix=f"eval-{case_id}-") as tmp:
        vault_root = Path(tmp) / "vault"
        vault_root.mkdir(parents=True, exist_ok=True)

        # Materialize the fixture (best-effort — empty / missing is fine).
        setup = case.get("vault_setup") or {}
        if setup:
            domain_hint = (case.get("intent") or "").split(".")[0] or "kernel"
            try:
                materialize_vault_setup(
                    setup,
                    vault_root=vault_root,
                    domain=domain_hint,
                )
            except Exception as err:  # noqa: BLE001
                # A bad fixture should fail the case, not the whole run.
                status = "error"
                error = f"vault_setup failed: {err}"

        if status == "ok":
            try:
                result = invoke_case(
                    case,
                    config_label=config_label,
                    vault_root=vault_root,
                    project_root=project_root,
                )
                reply = result.get("reply", "")
                tokens_in = int(result.get("tokens_in", 0))
                tokens_out = int(result.get("tokens_out", 0))
                tool_calls = list(result.get("tool_calls") or [])
                invoker_duration_ms = int(result.get("duration_ms", 0))
                audit_lines = list(result.get("audit_lines") or [])
                # Pre-existing tool calls in the audit lines feed the report's
                # tool-palette delta. Prefer the explicit ``tool_calls`` list.
                status = result.get("status", "ok")
            except Exception as err:  # noqa: BLE001
                status = "error"
                error = str(err)

    if audit_lines:
        _write_audit_lines(audit_path, audit_lines)

    duration_ms = int((time.monotonic() - started) * 1000) or invoker_duration_ms

    row: dict[str, Any] = {
        "case_id": case_id,
        "config": config_label,
        "reply": reply,
        "audit_path": str(audit_path),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "total_tokens": tokens_in + tokens_out,
        "tool_calls": tool_calls,
        "duration_ms": duration_ms,
        "status": status,
    }
    if error is not None:
        row["error"] = error
    return row


def run_cases(
    *,
    cases: list[dict[str, Any]],
    results_dir: str | os.PathLike[str],
    invoke_case: Callable[..., dict[str, Any]],
    project_root: str | os.PathLike[str],
    timestamp: str | None = None,
    limit: int | None = None,
) -> dict[str, Path]:
    """Execute every case under both configs; write per-config results JSON.

    Returns a mapping ``{config: Path}`` of the result files written.
    """
    results_root = Path(results_dir)
    results_root.mkdir(parents=True, exist_ok=True)
    audit_root = results_root / "_audit_per_case"
    audit_root.mkdir(parents=True, exist_ok=True)
    project = Path(project_root)
    ts = timestamp or _now_timestamp()

    if limit is not None:
        cases = cases[: max(0, int(limit))]

    written: dict[str, Path] = {}
    for cfg in CONFIGS:
        rows = [
            _run_one(
                case,
                config_label=cfg,
                invoke_case=invoke_case,
                project_root=project,
                audit_dir=audit_root,
            )
            for case in cases
        ]
        target = results_root / f"{cfg}-{ts}.json"
        target.write_text(
            json.dumps(rows, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        written[cfg] = target
    return written


# ---------------------------------------------------------------------------
# Default invoker (for production use of ``python -m eval.run``)
# ---------------------------------------------------------------------------


def _default_invoke_case(
    case: dict[str, Any],
    *,
    config_label: str,
    vault_root: Path,
    project_root: Path,
) -> dict[str, Any]:
    """Best-effort default invoker.

    Production wiring of this against the real orchestrator + LLM is out
    of scope for issue #13 — the harness contract is the ``invoke_case``
    callable. The default here returns a placeholder row so that running
    ``python -m eval.run`` end-to-end without a wired invoker still
    produces a structurally-correct results file (with ``status=skipped``).
    """
    return {
        "reply": "<skipped — no invoker wired>",
        "tokens_in": 0,
        "tokens_out": 0,
        "tool_calls": [],
        "duration_ms": 0,
        "audit_lines": [],
        "status": "skipped",
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval.run",
        description=(
            "Head-to-head eval harness — runs every case under default + "
            "baseline configs and writes per-config result JSON."
        ),
    )
    parser.add_argument(
        "--cases",
        default=None,
        help="Optional glob restricting which case files to load (e.g. "
        "'domains/fitness/eval/cases.jsonl').",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Run at most N cases (post-discovery).",
    )
    parser.add_argument(
        "--results-dir",
        default="eval/results",
        help="Where to write per-config result JSON files.",
    )
    return parser


def _filter_by_glob(cases: list[dict[str, Any]], pattern: str) -> list[dict[str, Any]]:
    """Keep only cases whose ``_source`` matches the glob pattern."""
    import fnmatch

    return [c for c in cases if fnmatch.fnmatch(c.get("_source", ""), pattern)]


def main(argv: list[str] | None = None) -> int:
    """``python -m eval.run`` entrypoint."""
    args = _build_parser().parse_args(argv)
    project_root = Path.cwd()

    cases = discover_cases(project_root=project_root)
    if args.cases:
        cases = _filter_by_glob(cases, args.cases)

    written = run_cases(
        cases=cases,
        results_dir=project_root / args.results_dir,
        invoke_case=_default_invoke_case,
        project_root=project_root,
        limit=args.limit,
    )
    for cfg, path in written.items():
        sys.stdout.write(f"wrote {cfg} results -> {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
