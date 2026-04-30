"""YAML-driven intent classifier.

Reads every ``domains/*/domain.yaml`` at startup, builds a flat tuple of
registered intents, and (per turn) calls ``claude_runner.invoke`` with the
classifier prompt to map a free-form user message onto one of those intents.

The cardinal rule: this module is **data-driven**. Adding a new domain is
done by dropping a new ``domain.yaml`` under ``domains/<name>/`` — never by
editing this file. See ``CLAUDE.md`` for the full reasoning.

Anything the LLM emits that does not match a registered intent collapses to
the constant ``FALLBACK_INTENT`` (``_inbox.fallback``). Misroutes are
silently triaged weekly out of ``vault/_inbox/``; that is the safety net.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Protocol

import yaml

from kernel.claude_runner import ClaudeResponse, invoke as claude_invoke

__all__ = [
    "Classifier",
    "FALLBACK_INTENT",
    "discover_intents",
]


FALLBACK_INTENT = "_inbox.fallback"

# The classifier prompt template lives next to this module; resolve it once
# at import time so the path stays stable regardless of CWD.
_CLASSIFIER_PROMPT_PATH = Path(__file__).parent / "prompts" / "classifier.md"

# Project-default domains root. Tests pass an explicit ``tmp_path`` so the
# default is only used in production.
DEFAULT_DOMAINS_ROOT = Path("domains")


class _ClaudeInvoker(Protocol):
    """The subset of the ``claude_runner`` API the classifier needs."""

    def __call__(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> ClaudeResponse: ...


def discover_intents(domains_root: str | os.PathLike[str]) -> tuple[str, ...]:
    """Return every intent declared in ``domains/<name>/domain.yaml`` files.

    Order is by domain directory name then by declaration order within the
    YAML so the registry is deterministic across runs.
    """
    root = Path(domains_root)
    if not root.exists():
        return ()

    discovered: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        domain_yaml = child / "domain.yaml"
        if not domain_yaml.exists():
            continue
        try:
            payload = yaml.safe_load(domain_yaml.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as err:
            raise RuntimeError(f"could not parse {domain_yaml}: {err}") from err
        intents = payload.get("intents") or []
        for intent in intents:
            if isinstance(intent, str) and intent not in discovered:
                discovered.append(intent)

    return tuple(discovered)


def _load_prompt_template() -> str:
    """Read the classifier prompt body from the kernel prompts dir."""
    if not _CLASSIFIER_PROMPT_PATH.exists():
        return ""
    return _CLASSIFIER_PROMPT_PATH.read_text(encoding="utf-8")


def _normalize_label(raw: str) -> str:
    """Strip whitespace + common code-fence wrappers an LLM might add."""
    text = raw.strip()
    if text.startswith("```"):
        # Drop opening fence (with optional language tag) and trailing fence.
        lines = [line for line in text.splitlines() if not line.startswith("```")]
        text = "\n".join(lines).strip()
    return text


def _compose_prompt(template: str, intents: tuple[str, ...], message: str) -> str:
    """Build the LLM prompt: template + intent list + user message."""
    intent_lines = "\n".join(f"- {intent}" for intent in intents)
    return (
        f"{template}\n\n"
        f"# Registered intents\n\n"
        f"{intent_lines}\n\n"
        f"# User message\n\n"
        f"{message}\n"
    )


class Classifier:
    """LLM-backed router from free-form text to a registered intent label."""

    def __init__(
        self,
        *,
        domains_root: str | os.PathLike[str] = DEFAULT_DOMAINS_ROOT,
        invoker: Optional[_ClaudeInvoker] = None,
        prompt_template: Optional[str] = None,
    ) -> None:
        self._intents = discover_intents(domains_root)
        self._invoker = invoker or claude_invoke
        self._prompt_template = (
            prompt_template if prompt_template is not None else _load_prompt_template()
        )

    @property
    def intents(self) -> tuple[str, ...]:
        """The discovered intent registry. Useful for tests + debugging."""
        return self._intents

    def classify(self, message: str) -> str:
        """Map ``message`` to a registered intent label or to ``FALLBACK_INTENT``."""
        prompt = _compose_prompt(self._prompt_template, self._intents, message)
        response = self._invoker(prompt, system_prompt=None)
        candidate = _normalize_label(response.text)
        if candidate in self._intents:
            return candidate
        return FALLBACK_INTENT
