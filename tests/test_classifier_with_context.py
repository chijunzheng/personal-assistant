"""Tests for the classifier's chat-context wiring (issue #28).

These tests pin the contracts that close the loop on the ADR at
``docs/decisions/2026-05-10-chat-context-and-capabilities.md``:

  1. **Backward compatibility.** Calling ``classify`` with no
     ``chat_context`` (or an empty ``ChatContext()``) yields a prompt
     byte-for-byte identical to the pre-#28 baseline so every existing
     classifier test keeps passing without modification.
  2. **Referential follow-ups resolve.** When the prior turn's bot reply
     announced a generated plan, a short follow-up like
     *"Yes, walk me through it"* arrives at the LLM with the antecedent
     visible in the prompt — and a realistic LLM response collapses to
     a real domain intent rather than ``FALLBACK_INTENT``.
  3. **Truncation budget holds.** A 10000-char prior turn doesn't blow
     the classifier's prompt budget — per-turn bodies are clipped and
     the assembled prompt stays under ~2500 chars.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from kernel.chat_context import ChatContext
from kernel.chat_log import ChatTurn
from kernel.classifier import (
    FALLBACK_INTENT,
    Classifier,
)
from kernel.claude_runner import ClaudeResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_domain(domains_root: Path, name: str, intents: list[str]) -> None:
    """Drop a minimal ``domain.yaml`` under ``domains_root/<name>/``."""
    domain_dir = domains_root / name
    domain_dir.mkdir(parents=True, exist_ok=True)
    intents_yaml = "\n".join(f"  - {intent}" for intent in intents)
    (domain_dir / "domain.yaml").write_text(
        f"name: {name}\n"
        f"description: \"test domain\"\n"
        f"intents:\n{intents_yaml}\n",
        encoding="utf-8",
    )


class _PromptCapturingInvoker:
    """Capture every prompt the classifier hands the LLM, return a fixed label."""

    def __init__(self, *, label: str = "_inbox.fallback") -> None:
        self.prompts: list[str] = []
        self._label = label

    def __call__(self, prompt: str, *, system_prompt=None) -> ClaudeResponse:
        self.prompts.append(prompt)
        return ClaudeResponse(
            text=self._label,
            tokens_in=1,
            tokens_out=1,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )


def _turn(
    *, user_msg: str, bot_reply: str, intent: str = "", chat_id: str = "c1"
) -> ChatTurn:
    """Build a ChatTurn without going through the chat_log writer.

    The classifier only consumes the in-memory shape — the on-disk
    sha256 isn't load-bearing here, so we synthesize a stable id
    locally to keep the test focused on prompt assembly.
    """
    ts = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc).isoformat()
    return ChatTurn(
        id=f"turn-{hash((user_msg, bot_reply))}",
        ts=ts,
        chat_id=chat_id,
        user_msg=user_msg,
        bot_reply=bot_reply,
        intent=intent,
    )


# ---------------------------------------------------------------------------
# 1. Empty / None chat_context = byte-for-byte identical prompt
# ---------------------------------------------------------------------------


def test_classify_no_chat_context_kwarg_matches_pre_28_prompt(tmp_path: Path) -> None:
    """Calling ``classify(message)`` with no kwarg must produce the legacy prompt.

    This pins backward compatibility for every callsite that hasn't
    adopted the new kwarg yet — the orchestrator wiring (#28) is the
    only intended caller, and existing tests must keep passing
    without modification.
    """
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "fitness", ["fitness.workout_log"])

    invoker_a = _PromptCapturingInvoker(label="fitness.workout_log")
    invoker_b = _PromptCapturingInvoker(label="fitness.workout_log")

    classifier_a = Classifier(
        domains_root=domains_root,
        invoker=invoker_a,
        prompt_template="TEMPLATE",
    )
    classifier_b = Classifier(
        domains_root=domains_root,
        invoker=invoker_b,
        prompt_template="TEMPLATE",
    )

    intent_a = classifier_a.classify("did 5x5 squats")
    intent_b = classifier_b.classify("did 5x5 squats", chat_context=None)

    assert intent_a == intent_b == "fitness.workout_log"
    assert invoker_a.prompts == invoker_b.prompts
    # Sanity: no preamble leaked in via the None-context branch.
    assert "## Recent conversation" not in invoker_b.prompts[0]


def test_classify_empty_chat_context_matches_pre_28_prompt(tmp_path: Path) -> None:
    """An empty ``ChatContext()`` must produce the same prompt as ``None``.

    "Empty" means ``recent_turns == ()`` — the most common state for a
    brand-new chat. The classifier must not alter its prompt shape in
    that case so existing tests + golden eval cases stay stable.
    """
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "fitness", ["fitness.workout_log"])

    invoker_none = _PromptCapturingInvoker(label="fitness.workout_log")
    invoker_empty = _PromptCapturingInvoker(label="fitness.workout_log")

    Classifier(
        domains_root=domains_root,
        invoker=invoker_none,
        prompt_template="TEMPLATE",
    ).classify("did 5x5 squats")

    Classifier(
        domains_root=domains_root,
        invoker=invoker_empty,
        prompt_template="TEMPLATE",
    ).classify("did 5x5 squats", chat_context=ChatContext())

    assert invoker_none.prompts == invoker_empty.prompts
    assert "## Recent conversation" not in invoker_empty.prompts[0]


# ---------------------------------------------------------------------------
# 2. Populated chat_context resolves a referential follow-up
# ---------------------------------------------------------------------------


def test_classify_with_recent_plan_turn_includes_preamble_in_prompt(
    tmp_path: Path,
) -> None:
    """The verbatim antecedent reaches the LLM as a ``## Recent conversation`` block.

    Without this, *"Yes, walk me through it"* arrives at the LLM with
    no context and the model has no signal beyond the literal four
    words — exactly the failure mode the ADR motivates fixing.
    """
    domains_root = tmp_path / "domains"
    _write_domain(
        domains_root,
        "fitness",
        ["fitness.nutrition_plan", "fitness.workout_plan", "fitness.query"],
    )

    chat_context = ChatContext(
        recent_turns=(
            _turn(
                user_msg="generate me a nutrition plan for today",
                bot_reply=(
                    "Done! Saved your nutrition plan to "
                    "fitness/plans/2026-05-10-nutrition-recomp.md."
                ),
                intent="fitness.nutrition_plan",
            ),
        ),
    )

    invoker = _PromptCapturingInvoker(label="fitness.query")
    classifier = Classifier(
        domains_root=domains_root,
        invoker=invoker,
        prompt_template="TEMPLATE",
    )

    intent = classifier.classify(
        "Yes, walk me through it",
        chat_context=chat_context,
    )

    assert intent == "fitness.query"
    assert intent != FALLBACK_INTENT
    assert len(invoker.prompts) == 1
    prompt = invoker.prompts[0]
    assert "## Recent conversation" in prompt
    assert "generate me a nutrition plan for today" in prompt
    assert "nutrition plan" in prompt
    # Preamble sits BEFORE the rules / intents / message blocks so the
    # model reads the antecedent first.
    assert prompt.index("## Recent conversation") < prompt.index(
        "# Registered intents"
    )
    assert prompt.index("## Recent conversation") < prompt.index("# User message")


def test_classify_with_only_recent_turns_uses_classifier_window(
    tmp_path: Path,
) -> None:
    """Only the last 3 turns reach the prompt — older turns are dropped.

    Handlers consume up to 5 turns; the classifier intentionally caps
    at a tighter 3-turn window because routing decisions need less
    context than a full handler answer and the per-turn classify
    prompt budget is the hottest path in the kernel.
    """
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "journal", ["journal.capture", "journal.query"])

    chat_context = ChatContext(
        recent_turns=tuple(
            _turn(
                user_msg=f"older user {i}",
                bot_reply=f"older bot {i}",
                intent="journal.capture",
            )
            for i in range(5)
        ),
    )

    invoker = _PromptCapturingInvoker(label="journal.query")
    Classifier(
        domains_root=domains_root,
        invoker=invoker,
        prompt_template="TEMPLATE",
    ).classify("recall that?", chat_context=chat_context)

    prompt = invoker.prompts[0]
    # The two oldest turns must be dropped from the classifier window.
    assert "older user 0" not in prompt
    assert "older user 1" not in prompt
    # The three most-recent turns must survive.
    assert "older user 2" in prompt
    assert "older user 3" in prompt
    assert "older user 4" in prompt


def test_classify_unknown_intent_still_falls_back(tmp_path: Path) -> None:
    """Even with chat context, an unregistered LLM label collapses to fallback.

    The chat-context preamble nudges the model toward a real intent,
    but the registry-membership invariant is unchanged: the classifier
    only ever returns a label that exists in the discovered registry,
    or ``_inbox.fallback``. This protects downstream dispatch from
    routing on a hallucinated label.
    """
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "fitness", ["fitness.nutrition_plan"])

    chat_context = ChatContext(
        recent_turns=(
            _turn(
                user_msg="generate me a nutrition plan",
                bot_reply="Done! Saved your nutrition plan.",
                intent="fitness.nutrition_plan",
            ),
        ),
    )

    invoker = _PromptCapturingInvoker(label="fitness.query")  # not registered
    intent = Classifier(
        domains_root=domains_root,
        invoker=invoker,
        prompt_template="TEMPLATE",
    ).classify("yes please", chat_context=chat_context)

    assert intent == FALLBACK_INTENT


# ---------------------------------------------------------------------------
# 3. Truncation: long bodies don't blow the classifier prompt budget
# ---------------------------------------------------------------------------


def test_classify_with_huge_recent_turns_stays_under_budget(tmp_path: Path) -> None:
    """A 10000-char prior turn must not blow the classifier prompt budget.

    The classifier prompt is the hottest path — it runs every turn,
    including for messages that ultimately route to ``_inbox.fallback``.
    Long prior bodies must be truncated to keep latency + token spend
    bounded. We pin the assembled-prompt size at a conservative ceiling
    so a regression here is loud.
    """
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "journal", ["journal.capture"])

    huge_user_msg = "u" * 10000
    huge_bot_reply = "b" * 10000

    chat_context = ChatContext(
        recent_turns=(
            _turn(user_msg=huge_user_msg, bot_reply=huge_bot_reply),
            _turn(user_msg=huge_user_msg, bot_reply=huge_bot_reply),
            _turn(user_msg=huge_user_msg, bot_reply=huge_bot_reply),
        ),
    )

    invoker = _PromptCapturingInvoker(label="journal.capture")
    Classifier(
        domains_root=domains_root,
        invoker=invoker,
        prompt_template="TEMPLATE",
    ).classify("a thought", chat_context=chat_context)

    prompt = invoker.prompts[0]
    # Generous ceiling — the classifier preamble + rules + intents +
    # message + (3 turns * ~300 chars truncated) should land well
    # under 2500 chars.
    assert len(prompt) < 2500, (
        f"classifier prompt too large ({len(prompt)} chars); "
        f"truncation likely regressed"
    )
    # And the per-turn truncation must have actually happened — no raw
    # 10000-char body should leak through.
    assert huge_user_msg not in prompt
    assert huge_bot_reply not in prompt
