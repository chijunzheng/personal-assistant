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
from dataclasses import replace
from pathlib import Path
from typing import Optional, Protocol

import yaml

from kernel.chat_context import ChatContext, compose_recent_preamble
from kernel.claude_runner import ClaudeResponse, invoke as claude_invoke

__all__ = [
    "Classifier",
    "FALLBACK_INTENT",
    "discover_intents",
]


FALLBACK_INTENT = "_inbox.fallback"

# Per the ADR (docs/decisions/2026-05-10-chat-context-and-capabilities.md),
# the classifier sees a *narrower* slice of chat context than handlers do
# — three turns is enough to disambiguate a referential follow-up like
# *"yes, walk me through it"* without inflating the per-turn classify
# prompt. Handler prompts use the full 5-turn window.
_CLASSIFIER_RECENT_TURNS = 3
# Per-turn body truncation budgets keep the assembled prompt snappy even
# when the user dumped a 10000-char wall of text the prior turn. The
# classifier doesn't need verbatim bodies — it needs the shape ("the bot
# saved a plan", "the user asked for X") to resolve a referent.
_USER_MSG_BUDGET = 100
_BOT_REPLY_BUDGET = 200
# Suffix appended when a body is truncated so the LLM can tell at a
# glance that the visible prefix is partial.
_TRUNCATION_SUFFIX = "..."

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


def _truncate(text: str, budget: int) -> str:
    """Trim ``text`` to ``budget`` chars, marking truncation explicitly.

    The classifier needs *shape* (what the prior turn was about), not
    verbatim content — so a hard truncation with a trailing marker is
    the right tradeoff. Returns the input unchanged when it already
    fits inside the budget.
    """
    if len(text) <= budget:
        return text
    keep = max(0, budget - len(_TRUNCATION_SUFFIX))
    return text[:keep] + _TRUNCATION_SUFFIX


def _trim_chat_context(chat_context: ChatContext) -> ChatContext:
    """Cap to the classifier window + truncate per-turn bodies in place-of.

    Returns a *new* ``ChatContext`` (immutable invariant) carrying at
    most ``_CLASSIFIER_RECENT_TURNS`` turns whose bodies have been
    truncated to the classifier's tighter per-turn budgets. The
    ``session_summary`` field is left alone — the classifier currently
    only consumes ``recent_turns``, but preserving the rest of the
    value keeps future wiring (e.g., session-aware fallback) trivial.
    """
    trimmed_turns = chat_context.recent_turns[-_CLASSIFIER_RECENT_TURNS:]
    truncated_turns = tuple(
        replace(
            turn,
            user_msg=_truncate(turn.user_msg, _USER_MSG_BUDGET),
            bot_reply=_truncate(turn.bot_reply, _BOT_REPLY_BUDGET),
        )
        for turn in trimmed_turns
    )
    return ChatContext(
        recent_turns=truncated_turns,
        session_summary=chat_context.session_summary,
    )


def _compose_prompt(
    template: str,
    intents: tuple[str, ...],
    message: str,
    *,
    chat_context: Optional[ChatContext] = None,
) -> str:
    """Build the LLM prompt: template + intent list + user message.

    When ``chat_context`` is non-empty, prepend the canonical
    ``## Recent conversation`` preamble so the classifier prompt
    (and the rest of the kernel that folds chat context into LLM
    prompts) speaks one shape. Empty / ``None`` context yields a
    prompt byte-for-byte identical to the issue-#26 baseline so
    existing classifier behavior is preserved.
    """
    intent_lines = "\n".join(f"- {intent}" for intent in intents)
    base = (
        f"{template}\n\n"
        f"# Registered intents\n\n"
        f"{intent_lines}\n\n"
        f"# User message\n\n"
        f"{message}\n"
    )
    if chat_context is None or not chat_context.recent_turns:
        return base
    trimmed = _trim_chat_context(chat_context)
    preamble = compose_recent_preamble(trimmed.recent_turns)
    if not preamble:
        return base
    return f"{preamble}{base}"


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

    def classify(
        self,
        message: str,
        *,
        chat_context: Optional[ChatContext] = None,
    ) -> str:
        """Map ``message`` to a registered intent label or to ``FALLBACK_INTENT``.

        ``chat_context`` is the per-turn ``ChatContext`` the orchestrator
        loaded at the top of ``handle_message``. When non-empty, the last
        few verbatim turns are prepended to the classifier prompt as a
        ``## Recent conversation`` preamble so referential follow-ups
        like *"yes, walk me through it"* resolve to a meaningful intent
        instead of falling through to ``FALLBACK_INTENT``. When ``None``
        or empty, the prompt is byte-for-byte identical to the pre-#28
        baseline — existing classifier behavior is preserved.

        If the LLM returns an intent label that is not in the discovered
        registry, the classifier still collapses to ``FALLBACK_INTENT``
        — the chat-context preamble nudges the model toward a real
        intent but does not relax the registry-membership invariant.
        """
        prompt = _compose_prompt(
            self._prompt_template,
            self._intents,
            message,
            chat_context=chat_context,
        )
        response = self._invoker(prompt, system_prompt=None)
        candidate = _normalize_label(response.text)
        if candidate in self._intents:
            return candidate
        return FALLBACK_INTENT
