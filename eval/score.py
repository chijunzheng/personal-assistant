"""Manual 5-dim Likert scorer (v1).

Loads two paired result files (one per config) and asks the human evaluator
to score each case across the five dimensions:

  * accuracy    — does the answer match the ground truth?
  * grounding   — does the answer cite vault evidence?
  * conciseness — is it as short as it can be while still being complete?
  * connection  — does it integrate cross-domain signal where relevant?
  * trust       — would the user act on this answer without verifying?

Each dimension is on a 1..5 Likert.

For test-friendliness (and for CI runs that don't have a human in the loop),
``--non-interactive`` accepts a JSON pre-fill keyed by ``{case_id: {config:
{dim: score}}}``.

The ``--judge llm`` flag is a v2 stub: v1 emits a warning and falls back to
the manual / non-interactive path. No actual LLM call is made here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "DIMENSIONS",
    "score_paired",
    "main",
]


DIMENSIONS: tuple[str, ...] = (
    "accuracy",
    "grounding",
    "conciseness",
    "connection",
    "trust",
)
LIKERT_MIN = 1
LIKERT_MAX = 5


def _load_json_file(path: Path) -> Any:
    """Read a JSON file; raise a clear error if it doesn't parse."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise ValueError(f"could not load {path}: {err}") from err


def _validate_likert(scores: Mapping[str, Any], *, case_id: str, config: str) -> dict[str, int]:
    """Validate one ``{dim: score}`` block — all 5 dims, all in 1..5."""
    validated: dict[str, int] = {}
    for dim in DIMENSIONS:
        if dim not in scores:
            raise ValueError(
                f"case {case_id} ({config}) missing score for dimension '{dim}'"
            )
        value = scores[dim]
        try:
            ivalue = int(value)
        except (TypeError, ValueError) as err:
            raise ValueError(
                f"case {case_id} ({config}) dim '{dim}': non-integer score {value!r}"
            ) from err
        if not (LIKERT_MIN <= ivalue <= LIKERT_MAX):
            raise ValueError(
                f"case {case_id} ({config}) dim '{dim}': score {ivalue} "
                f"out of range [{LIKERT_MIN}, {LIKERT_MAX}]"
            )
        validated[dim] = ivalue
    return validated


def _case_ids_in_order(rows: list[dict[str, Any]]) -> list[str]:
    """Return case ids in the order they appear in the result file (stable)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for row in rows:
        cid = row.get("case_id")
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        ordered.append(cid)
    return ordered


def _interactive_prompt_for_case(case_id: str, config: str, reply: str) -> dict[str, int]:
    """Prompt the human for one case+config, returning a {dim: score} dict.

    Production CLI path. Tests use ``--non-interactive`` and never hit this.
    """
    sys.stdout.write(f"\n--- case {case_id} | config={config} ---\n")
    sys.stdout.write(f"reply: {reply}\n")
    sys.stdout.flush()
    scores: dict[str, int] = {}
    for dim in DIMENSIONS:
        while True:
            raw = input(f"  {dim} (1-5): ").strip()
            try:
                ivalue = int(raw)
            except ValueError:
                sys.stdout.write("    please enter an integer 1..5\n")
                continue
            if not (LIKERT_MIN <= ivalue <= LIKERT_MAX):
                sys.stdout.write(f"    out of range — must be {LIKERT_MIN}..{LIKERT_MAX}\n")
                continue
            scores[dim] = ivalue
            break
    return scores


def score_paired(
    *,
    default_path: str | os.PathLike[str],
    baseline_path: str | os.PathLike[str],
    out_path: str | os.PathLike[str],
    non_interactive_path: str | os.PathLike[str] | None = None,
    judge: str = "manual",
    interactive_prompt=_interactive_prompt_for_case,
) -> Path:
    """Score a paired (default, baseline) results pair on five Likert dims.

    Returns the path of the scored JSON written to disk.

    The output schema is::

        {
          "<case_id>": {
            "default":  {"accuracy": 5, "grounding": 4, ...},
            "baseline": {"accuracy": 3, "grounding": 2, ...},
            "_status":  "ok" | "skipped",
            "_meta":    {"reply_default": "...", "reply_baseline": "..."}
          },
          ...
        }
    """
    default_rows = _load_json_file(Path(default_path))
    baseline_rows = _load_json_file(Path(baseline_path))

    if not isinstance(default_rows, list) or not isinstance(baseline_rows, list):
        raise ValueError("paired result files must be JSON arrays of row dicts")

    # Index by (case_id) for both pair members.
    by_id_default = {r.get("case_id"): r for r in default_rows}
    by_id_baseline = {r.get("case_id"): r for r in baseline_rows}

    case_ids = _case_ids_in_order(default_rows)

    # v2 LLM-as-judge stub — print a warning and fall through to manual scoring.
    if judge == "llm":
        sys.stderr.write(
            "warning: LLM-as-judge is v2 and not implemented yet; "
            "v1 only supports manual scoring. Falling back to manual.\n"
        )

    prefill: dict[str, Any] = {}
    if non_interactive_path is not None:
        prefill = _load_json_file(Path(non_interactive_path))
        if not isinstance(prefill, dict):
            raise ValueError("non-interactive prefill must be a JSON object")

    scored: dict[str, Any] = {}

    for cid in case_ids:
        default_row = by_id_default.get(cid, {})
        baseline_row = by_id_baseline.get(cid, {})
        case_block: dict[str, Any] = {
            "_meta": {
                "reply_default": default_row.get("reply", ""),
                "reply_baseline": baseline_row.get("reply", ""),
            }
        }

        case_prefill = prefill.get(cid) if non_interactive_path is not None else None

        if non_interactive_path is not None and case_prefill is None:
            # No scores supplied for this case — record skipped without crashing.
            case_block["_status"] = "skipped"
            scored[cid] = case_block
            continue

        try:
            if case_prefill is not None:
                case_block["default"] = _validate_likert(
                    case_prefill.get("default", {}), case_id=cid, config="default"
                )
                case_block["baseline"] = _validate_likert(
                    case_prefill.get("baseline", {}), case_id=cid, config="baseline"
                )
            else:
                # Interactive path — production only.
                case_block["default"] = _validate_likert(
                    interactive_prompt(cid, "default", default_row.get("reply", "")),
                    case_id=cid,
                    config="default",
                )
                case_block["baseline"] = _validate_likert(
                    interactive_prompt(cid, "baseline", baseline_row.get("reply", "")),
                    case_id=cid,
                    config="baseline",
                )
            case_block["_status"] = "ok"
        except ValueError:
            # Re-raise validation errors so tests can pin them.
            raise

        scored[cid] = case_block

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval.score",
        description="Manual 5-dim Likert scorer for paired eval result files (v1).",
    )
    parser.add_argument("--default", required=True, help="Path to default-config results JSON")
    parser.add_argument("--baseline", required=True, help="Path to baseline-config results JSON")
    parser.add_argument("--out", required=True, help="Where to write the scored JSON")
    parser.add_argument(
        "--non-interactive",
        default=None,
        help=(
            "Pre-recorded scores JSON file (skip the interactive prompt — "
            "useful for replay + CI tests)."
        ),
    )
    parser.add_argument(
        "--judge",
        default="manual",
        choices=["manual", "llm"],
        help="manual (v1, default) or llm (v2 stub — emits warning).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    score_paired(
        default_path=args.default,
        baseline_path=args.baseline,
        out_path=args.out,
        non_interactive_path=args.non_interactive,
        judge=args.judge,
    )
    sys.stdout.write(f"wrote scored output -> {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
