"""Validity tests for per-domain eval cases (issue #14).

Every plugin under ``domains/<name>/`` must ship its own
``eval/cases.jsonl`` with at least 5 head-to-head cases. These tests are
data-driven: parameterized over the discovered cases.jsonl files so a
new domain plugin automatically gets validated when it lands.

Failure modes covered:
  * file missing or empty
  * fewer than 5 cases
  * malformed JSON on any line
  * missing required field on any case
  * duplicate ``id`` within one file
  * ``intent`` references an intent NOT declared in the matching
    ``domain.yaml`` (the registration contract)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOMAINS_DIR = PROJECT_ROOT / "domains"

REQUIRED_FIELDS = ("id", "tags", "intent", "input", "score_dims", "expectation_vs_baseline")
MIN_CASES_PER_DOMAIN = 5


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file into a list of dicts.

    Returns an empty list if the file does not exist. Each non-empty line
    must be a valid JSON object — we deliberately raise here (vs the
    harness which silently skips malformed lines) because *authorship*
    validity is the property under test.
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as err:
                pytest.fail(
                    f"{path} line {lineno} is not valid JSON: {err.msg}"
                )
    return rows


def _discover_domain_cases() -> list[tuple[str, Path]]:
    """Return ``[(domain_name, cases_path), ...]`` for every plugin with a cases file.

    Each entry parameterizes one test instance — pytest reports failures
    keyed by domain name so it's obvious which file failed.
    """
    if not DOMAINS_DIR.exists():
        return []
    found: list[tuple[str, Path]] = []
    for child in sorted(DOMAINS_DIR.iterdir()):
        if not child.is_dir():
            continue
        cases = child / "eval" / "cases.jsonl"
        if cases.exists():
            found.append((child.name, cases))
    return found


def _load_domain_intents(domain_dir: Path) -> set[str]:
    """Return the declared intents from ``domain.yaml`` (or empty set on miss).

    The plugin contract declares intents in YAML at the domain root. Cases
    must reference one of those intents — drift between cases and YAML is
    a registration bug.
    """
    yaml_path = domain_dir / "domain.yaml"
    if not yaml_path.exists():
        return set()
    with open(yaml_path, "r", encoding="utf-8") as fh:
        spec = yaml.safe_load(fh) or {}
    intents = spec.get("intents") or []
    return {str(i).strip() for i in intents}


DOMAIN_CASES = _discover_domain_cases()
DOMAIN_IDS = [name for name, _ in DOMAIN_CASES]


# ---------------------------------------------------------------------------
# Existence + minimum-count
# ---------------------------------------------------------------------------


def test_at_least_one_domain_with_cases_exists() -> None:
    """The discovery helper finds at least one ``cases.jsonl`` under domains/.

    A regression here means the directory layout changed in a way that
    breaks the eval harness's auto-discovery — load-bearing safeguard.
    """
    assert DOMAIN_CASES, (
        "No domains/<name>/eval/cases.jsonl files found — issue #14's "
        "core invariant ('every plugin ships its own eval cases') is "
        "violated."
    )


@pytest.mark.parametrize(
    "domain_name,cases_path",
    DOMAIN_CASES,
    ids=DOMAIN_IDS,
)
def test_cases_file_is_non_empty(domain_name: str, cases_path: Path) -> None:
    """Each domain's cases file is non-empty on disk."""
    assert cases_path.stat().st_size > 0, (
        f"{cases_path} exists but is empty — every plugin must ship "
        f"at least {MIN_CASES_PER_DOMAIN} eval cases."
    )


@pytest.mark.parametrize(
    "domain_name,cases_path",
    DOMAIN_CASES,
    ids=DOMAIN_IDS,
)
def test_at_least_5_cases_per_domain(domain_name: str, cases_path: Path) -> None:
    """Each domain ships >= ``MIN_CASES_PER_DOMAIN`` cases (issue #14 acceptance)."""
    cases = _read_jsonl(cases_path)
    assert len(cases) >= MIN_CASES_PER_DOMAIN, (
        f"{domain_name} has only {len(cases)} eval cases at {cases_path} — "
        f"the 'no eval, no promotion' gate requires >= {MIN_CASES_PER_DOMAIN}."
    )


# ---------------------------------------------------------------------------
# Per-case shape
# ---------------------------------------------------------------------------


def _expand_case_params() -> list[tuple[str, Path, int, dict[str, Any]]]:
    """Flatten ``(domain, path, case_idx, case)`` for parameterization.

    Done at collection time so pytest reports each failing case by id.
    """
    expanded: list[tuple[str, Path, int, dict[str, Any]]] = []
    for domain_name, cases_path in DOMAIN_CASES:
        for idx, case in enumerate(_read_jsonl(cases_path)):
            expanded.append((domain_name, cases_path, idx, case))
    return expanded


CASE_PARAMS = _expand_case_params()
CASE_IDS = [
    f"{name}::{case.get('id', f'idx{idx}')}"
    for name, _, idx, case in CASE_PARAMS
]


@pytest.mark.parametrize(
    "domain_name,cases_path,case_idx,case",
    CASE_PARAMS,
    ids=CASE_IDS,
)
def test_case_has_required_fields(
    domain_name: str,
    cases_path: Path,
    case_idx: int,
    case: dict[str, Any],
) -> None:
    """Every case has the gold-standard fields from issue #14."""
    missing = [f for f in REQUIRED_FIELDS if f not in case]
    assert not missing, (
        f"{cases_path} case index {case_idx} (id={case.get('id', '<no-id>')}) "
        f"is missing required fields: {missing}. "
        f"Required: {list(REQUIRED_FIELDS)}."
    )


@pytest.mark.parametrize(
    "domain_name,cases_path,case_idx,case",
    CASE_PARAMS,
    ids=CASE_IDS,
)
def test_case_id_is_a_non_empty_string(
    domain_name: str,
    cases_path: Path,
    case_idx: int,
    case: dict[str, Any],
) -> None:
    """``id`` must be a non-empty string for dedupe + result-row keying."""
    cid = case.get("id")
    assert isinstance(cid, str) and cid.strip(), (
        f"{cases_path} case index {case_idx} has invalid id={cid!r}; "
        f"id must be a non-empty string."
    )


@pytest.mark.parametrize(
    "domain_name,cases_path,case_idx,case",
    CASE_PARAMS,
    ids=CASE_IDS,
)
def test_case_tags_is_a_list(
    domain_name: str,
    cases_path: Path,
    case_idx: int,
    case: dict[str, Any],
) -> None:
    """``tags`` is a list of strings (used for filtering eval slices)."""
    tags = case.get("tags")
    assert isinstance(tags, list), (
        f"{cases_path} case id={case.get('id')} has tags={tags!r}; "
        f"tags must be a list of strings."
    )
    bad = [t for t in tags if not isinstance(t, str)]
    assert not bad, (
        f"{cases_path} case id={case.get('id')} has non-string tags: {bad!r}."
    )


@pytest.mark.parametrize(
    "domain_name,cases_path,case_idx,case",
    CASE_PARAMS,
    ids=CASE_IDS,
)
def test_case_score_dims_is_a_list(
    domain_name: str,
    cases_path: Path,
    case_idx: int,
    case: dict[str, Any],
) -> None:
    """``score_dims`` lists which of the 5 Likert dims this case scores on."""
    dims = case.get("score_dims")
    assert isinstance(dims, list) and dims, (
        f"{cases_path} case id={case.get('id')} has score_dims={dims!r}; "
        f"score_dims must be a non-empty list."
    )
    allowed = {"accuracy", "grounding", "conciseness", "connection", "trust"}
    invalid = [d for d in dims if d not in allowed]
    assert not invalid, (
        f"{cases_path} case id={case.get('id')} has invalid score_dims: "
        f"{invalid!r}. Allowed: {sorted(allowed)}."
    )


# ---------------------------------------------------------------------------
# Per-file invariants (id uniqueness; intent registration)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain_name,cases_path",
    DOMAIN_CASES,
    ids=DOMAIN_IDS,
)
def test_case_ids_are_unique_within_file(
    domain_name: str,
    cases_path: Path,
) -> None:
    """Within one cases.jsonl, every ``id`` is unique."""
    cases = _read_jsonl(cases_path)
    ids = [c.get("id") for c in cases]
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for cid in ids:
        if cid is None:
            continue
        seen[cid] = seen.get(cid, 0) + 1
    duplicates = [cid for cid, count in seen.items() if count > 1]
    assert not duplicates, (
        f"{cases_path} has duplicate ids: {duplicates}. "
        f"Case ids must be unique within a file (the harness dedupes by id "
        f"globally too — collisions inside one file are an authoring error)."
    )


@pytest.mark.parametrize(
    "domain_name,cases_path",
    DOMAIN_CASES,
    ids=DOMAIN_IDS,
)
def test_case_intent_is_declared_in_domain_yaml(
    domain_name: str,
    cases_path: Path,
) -> None:
    """Every case's ``intent`` must be declared in the matching domain.yaml.

    The plugin contract: ``intents:`` lists every intent the classifier
    can dispatch to that domain. Cases referencing an undeclared intent
    indicate either a typo in the case or a missing YAML registration —
    both are authoring bugs the harness silently swallows otherwise.
    """
    domain_dir = cases_path.parent.parent
    declared = _load_domain_intents(domain_dir)
    cases = _read_jsonl(cases_path)

    unknown: list[tuple[str, str]] = []
    for case in cases:
        intent = case.get("intent")
        if not isinstance(intent, str):
            continue
        if declared and intent not in declared:
            unknown.append((case.get("id", "<no-id>"), intent))

    assert not unknown, (
        f"{cases_path} cases reference intents NOT declared in "
        f"{domain_dir / 'domain.yaml'}. Offending (id, intent) pairs: "
        f"{unknown}. Declared intents: {sorted(declared)}."
    )
