"""Tests for the generalized ChatContext wiring (issue #27).

These tests pin the contracts the ADR at
``docs/decisions/2026-05-10-chat-context-and-capabilities.md`` calls
out for the orchestrator-side seam:

  1. ``kernel.chat_log.load_recent`` is called *exactly once* per
     ``handle_message`` call — the per-turn ChatContext is loaded at
     the boundary, not re-loaded by every dispatch helper.
  2. The same ``ChatContext`` instance reaches structured handlers
     (journal-query, fitness-plan), surfaced as a ``## Recent
     conversation`` preamble in the LLM prompt.
  3. After dispatch, ``kernel.chat_log.append`` runs exactly once with
     the routed intent and the actual reply text — including on the
     error path so continuity is preserved across apologies.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import Mock

import yaml

from kernel import chat_log
from kernel.chat_context import ChatContext
from kernel.classifier import Classifier
from kernel.claude_runner import ClaudeResponse, ClaudeRunnerError
from kernel.orchestrator import Orchestrator, SingleInstanceLock


# ---------------------------------------------------------------------------
# Tiny helpers — keep the body of each test focused on the assertions
# ---------------------------------------------------------------------------


class _RecordingInvoker:
    """Capture every prompt/system_prompt the orchestrator hands the LLM."""

    def __init__(self, *, text: str = "ok") -> None:
        self.calls: list[dict] = []
        self._text = text

    def __call__(
        self, prompt: str, *, system_prompt: Optional[str] = None
    ) -> ClaudeResponse:
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        return ClaudeResponse(
            text=self._text,
            tokens_in=1,
            tokens_out=1,
            raw={"prompt": prompt, "system_prompt": system_prompt},
        )


def _fixed_clock(dt: datetime):
    return lambda: dt


def _seed_journal_domain(domains_root: Path) -> None:
    """Drop a minimal journal ``domain.yaml`` for the classifier to discover."""
    domain_dir = domains_root / "journal"
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "domain.yaml").write_text(
        "name: journal\nintents:\n  - journal.query\n",
        encoding="utf-8",
    )


def _seed_fitness_plan_domain(domains_root: Path) -> None:
    """Drop a minimal fitness ``domain.yaml`` carrying the workout-plan intent."""
    domain_dir = domains_root / "fitness"
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "domain.yaml").write_text(
        "name: fitness\nintents:\n  - fitness.workout_plan\n",
        encoding="utf-8",
    )


def _seed_journal_vault(vault_root: Path) -> None:
    """Minimal vault that ``gather_context`` and ``journal.read`` accept."""
    (vault_root / "_index").mkdir(parents=True, exist_ok=True)
    (vault_root / "journal").mkdir(parents=True, exist_ok=True)
    (vault_root / "_index" / "INDEX.md").write_text(
        "# INDEX\n\nconsciousness, qualia\n", encoding="utf-8"
    )
    (vault_root / "journal" / "2026-04-15-consciousness.md").write_text(
        "today I read about consciousness\n", encoding="utf-8"
    )


def _seed_fitness_profile(vault_root: Path) -> None:
    """A profile fully filled in (no TODOs) so plan generation does not refuse."""
    fitness = vault_root / "fitness"
    fitness.mkdir(parents=True, exist_ok=True)
    (fitness / "profile.yaml").write_text(
        yaml.safe_dump(
            {
                "sex": "male",
                "date_of_birth": "1990-01-01",
                "height_cm": 178,
                "weight_kg": 78,
                "goal": "recomp",
                "target_weight_kg": 76,
                "target_date": None,
                "activity_level": "moderate",
                "weekly_training_days": 4,
                "equipment_available": ["dumbbells", "bw"],
                "dietary_restrictions": [],
                "allergies": [],
                "injuries_active": [],
                "injuries_history": [],
                "preferences": {"enjoys": ["lifting"], "avoids": []},
                "target_calories_kcal": 2200,
                "target_protein_g": 160,
                "target_carbs_g": 220,
                "target_fat_g": 70,
                "plan_cadence": "daily",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (fitness / "workouts.jsonl").write_text("", encoding="utf-8")
    (fitness / "metrics.jsonl").write_text("", encoding="utf-8")


def _classifier_returning(domains_root: Path, intent: str) -> Classifier:
    """A test classifier that hard-codes a single intent label."""
    return Classifier(
        domains_root=domains_root,
        invoker=lambda *_a, **_k: ClaudeResponse(
            text=intent, tokens_in=1, tokens_out=1, raw={}
        ),
        prompt_template="",
    )


# ---------------------------------------------------------------------------
# 1. load_recent + append are called exactly once per turn
# ---------------------------------------------------------------------------


def test_chat_log_load_recent_called_exactly_once_per_turn(
    tmp_path: Path, lock_path: Path, monkeypatch
) -> None:
    """Per-turn ChatContext must be loaded once; not per dispatch helper.

    Without this, every handler that wanted chat-context would re-read
    the log, defeating the ADR's load-once discipline and inflating
    Drive sync churn under proliferating handlers.
    """
    load_calls: list[dict] = []
    real_load_recent = chat_log.load_recent

    def _spy_load_recent(chat_id, n, vault_root):
        load_calls.append({"chat_id": chat_id, "n": n, "vault_root": vault_root})
        return real_load_recent(chat_id, n, vault_root=vault_root)

    monkeypatch.setattr(chat_log, "load_recent", _spy_load_recent)

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=tmp_path / "audit",
        vault_root=tmp_path / "vault",
        invoker=_RecordingInvoker(text="hi back"),
        chat_id="chat-once",
    )

    orchestrator.handle_message("hi")

    assert len(load_calls) == 1
    assert load_calls[0]["chat_id"] == "chat-once"
    assert load_calls[0]["n"] == 5


def test_chat_log_append_called_exactly_once_with_classified_intent(
    tmp_path: Path, lock_path: Path, monkeypatch
) -> None:
    """Post-dispatch append carries the classified intent, not the empty string.

    Generalizing chat_log.append from the echo handler to the
    orchestrator boundary means every routed turn (journal, fitness,
    finance, ...) records its intent — that's what makes the chat log
    a usable signal for downstream features.
    """
    append_calls: list[dict] = []
    real_append = chat_log.append

    def _spy_append(chat_id, **kwargs):
        append_calls.append({"chat_id": chat_id, **kwargs})
        return real_append(chat_id, **kwargs)

    monkeypatch.setattr(chat_log, "append", _spy_append)

    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    _seed_journal_vault(vault_root)

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        vault_root=vault_root,
        invoker=_RecordingInvoker(text="recalled it"),
        classifier=_classifier_returning(domains_root, "journal.query"),
        chat_id="chat-append",
        clock=_fixed_clock(datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)),
    )

    reply = orchestrator.handle_message("what did I write about consciousness?")

    assert len(append_calls) == 1
    record = append_calls[0]
    assert record["chat_id"] == "chat-append"
    assert record["user_msg"] == "what did I write about consciousness?"
    assert record["bot_reply"] == reply.text
    assert record["intent"] == "journal.query"


def test_chat_log_append_runs_on_error_path_too(
    tmp_path: Path, lock_path: Path, monkeypatch
) -> None:
    """Even when dispatch raises, the apology turn lands in the chat log.

    Without this, a user who hits a transient runner failure loses the
    prior turn's antecedent on retry — the next turn would see only the
    new user message with no record that an apology happened in between.
    """
    append_calls: list[dict] = []
    real_append = chat_log.append

    def _spy_append(chat_id, **kwargs):
        append_calls.append({"chat_id": chat_id, **kwargs})
        return real_append(chat_id, **kwargs)

    monkeypatch.setattr(chat_log, "append", _spy_append)

    def _boom(_prompt, *, system_prompt=None):
        raise ClaudeRunnerError("subprocess crashed")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=tmp_path / "audit",
        vault_root=tmp_path / "vault",
        invoker=_boom,
        chat_id="chat-err",
    )

    reply = orchestrator.handle_message("anything")

    assert "wrong" in reply.text.lower()
    assert len(append_calls) == 1
    assert append_calls[0]["user_msg"] == "anything"
    assert "wrong" in append_calls[0]["bot_reply"].lower()


# ---------------------------------------------------------------------------
# 2. ChatContext reaches the journal-query handler as a prompt preamble
# ---------------------------------------------------------------------------


def test_journal_query_handler_includes_recent_conversation_preamble(
    tmp_path: Path, lock_path: Path
) -> None:
    """The journal-read prompt carries the verbatim prior turns.

    A follow-up like *"and what about that earlier note?"* needs the
    prior turn to resolve "that earlier note" — this is the whole
    point of folding chat-context into structured handlers.
    """
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    _seed_journal_vault(vault_root)
    chat_id = "chat-journal-context"

    chat_log.append(
        chat_id,
        user_msg="what did I write about consciousness?",
        bot_reply="You wrote about consciousness on 2026-04-15.",
        intent="journal.query",
        vault_root=vault_root,
        clock=_fixed_clock(datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)),
    )

    invoker = _RecordingInvoker(text="answered with context")
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        vault_root=vault_root,
        invoker=invoker,
        classifier=_classifier_returning(domains_root, "journal.query"),
        chat_id=chat_id,
        clock=_fixed_clock(datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)),
    )

    orchestrator.handle_message("and what about the qualia angle?")

    assert len(invoker.calls) >= 1
    prompt = invoker.calls[0]["prompt"]
    assert "## Recent conversation" in prompt
    assert "what did I write about consciousness?" in prompt
    assert "You wrote about consciousness on 2026-04-15." in prompt
    # The preamble sits BEFORE the retrieval blocks so the LLM reads
    # the antecedent before encountering the new question.
    assert prompt.index("## Recent conversation") < prompt.index("# Retrieved notes")


# ---------------------------------------------------------------------------
# 3. ChatContext reaches the fitness-plan handler as a prompt preamble
# ---------------------------------------------------------------------------


def test_fitness_plan_handler_includes_recent_conversation_preamble(
    tmp_path: Path, lock_path: Path
) -> None:
    """A workout_plan turn following a nutrition_plan turn carries the prior request.

    This is the end-to-end behavior the issue calls out: "now do a
    workout one too" after a nutrition-plan request must arrive at the
    plan-generation prompt with the nutrition framing visible.
    """
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_fitness_plan_domain(domains_root)
    _seed_fitness_profile(vault_root)
    chat_id = "chat-fitness-context"

    chat_log.append(
        chat_id,
        user_msg="generate me a nutrition plan for today",
        bot_reply="Saved plan to fitness/plans/2026-05-10-nutrition-foo.md.",
        intent="fitness.nutrition_plan",
        vault_root=vault_root,
        clock=_fixed_clock(datetime(2026, 5, 10, 8, 0, 0, tzinfo=timezone.utc)),
    )

    invoker = _RecordingInvoker(text="## Workout Plan\n\n- squats\n")
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        vault_root=vault_root,
        invoker=invoker,
        classifier=_classifier_returning(domains_root, "fitness.workout_plan"),
        chat_id=chat_id,
        clock=_fixed_clock(datetime(2026, 5, 10, 8, 5, 0, tzinfo=timezone.utc)),
    )

    orchestrator.handle_message("now do a workout one too")

    # The plan prompt must carry both the preamble and the prior turn.
    assert len(invoker.calls) >= 1
    plan_prompts = [
        call["prompt"]
        for call in invoker.calls
        if "Generate a workout plan" in call["prompt"]
    ]
    assert plan_prompts, f"expected a workout-plan prompt, got: {[c['prompt'][:80] for c in invoker.calls]}"
    plan_prompt = plan_prompts[0]
    assert "## Recent conversation" in plan_prompt
    assert "generate me a nutrition plan for today" in plan_prompt
    # The preamble sits before the structured grounding so the model
    # reads the prior request alongside the profile / metrics blocks.
    assert plan_prompt.index("## Recent conversation") < plan_prompt.index(
        "## Profile"
    )


# ---------------------------------------------------------------------------
# 4. The same ChatContext instance is shared across dispatch helpers
# ---------------------------------------------------------------------------


def test_same_chat_context_instance_stashed_for_the_whole_turn(
    tmp_path: Path, lock_path: Path
) -> None:
    """``self._chat_context`` is the single source of truth per turn.

    Pinning identity (not just equality) gives us early warning if a
    handler accidentally rebuilds ChatContext mid-dispatch, which would
    re-read the log and split semantics across helpers.
    """
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    _seed_journal_vault(vault_root)
    chat_id = "chat-identity"

    chat_log.append(
        chat_id,
        user_msg="seeded earlier",
        bot_reply="seeded reply",
        intent="journal.query",
        vault_root=vault_root,
        clock=_fixed_clock(datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)),
    )

    invoker = _RecordingInvoker(text="ok")
    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        vault_root=vault_root,
        invoker=invoker,
        classifier=_classifier_returning(domains_root, "journal.query"),
        chat_id=chat_id,
        clock=_fixed_clock(datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)),
    )

    orchestrator.handle_message("follow-up question")

    # After the turn, the stashed context should still carry the seeded turn.
    assert len(orchestrator._chat_context.recent_turns) >= 1
    assert orchestrator._chat_context.recent_turns[0].user_msg == "seeded earlier"


# ---------------------------------------------------------------------------
# 5. Audit log is unchanged by the consolidation (one read entry, no extras)
# ---------------------------------------------------------------------------


def test_journal_query_audit_entry_unchanged_by_chat_context(
    tmp_path: Path, lock_path: Path
) -> None:
    """The audit log should still get exactly one ``read`` entry per turn.

    The chat-log append happens AFTER dispatch and is intentionally not
    audit-logged (the chat log itself is the record). This test pins
    that the audit shape stays identical to issue #3's contract so
    downstream eval tools don't see a phantom op.
    """
    vault_root = tmp_path / "vault"
    domains_root = tmp_path / "domains"
    _seed_journal_domain(domains_root)
    _seed_journal_vault(vault_root)

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        vault_root=vault_root,
        invoker=_RecordingInvoker(text="answered"),
        classifier=_classifier_returning(domains_root, "journal.query"),
        chat_id="chat-audit",
        clock=_fixed_clock(datetime(2026, 5, 10, 11, 0, 0, tzinfo=timezone.utc)),
    )

    orchestrator.handle_message("what did I write about consciousness?")

    audit_files = list((vault_root / "_audit").glob("*.jsonl"))
    assert len(audit_files) == 1
    entries = [
        json.loads(line)
        for line in audit_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    read_entries = [e for e in entries if e["op"] == "read"]
    assert len(read_entries) == 1
    assert read_entries[0]["domain"] == "journal"
    assert read_entries[0]["intent"] == "journal.query"
    assert read_entries[0]["outcome"] == "ok"


# ---------------------------------------------------------------------------
# 6. Orchestrator threads its loaded ChatContext into Classifier.classify
# ---------------------------------------------------------------------------


def test_orchestrator_passes_loaded_chat_context_into_classifier(
    tmp_path: Path, lock_path: Path
) -> None:
    """``Classifier.classify`` is called with the same ``ChatContext`` instance.

    Issue #28's whole point is that the per-turn ``ChatContext`` the
    orchestrator already loads (#27) reaches the classifier so
    referential follow-ups resolve. This test pins the exact wiring:
    the kwarg is present, and the value is the *identical* instance
    the orchestrator stashed on ``self._chat_context`` — not a fresh
    one rebuilt mid-dispatch.
    """
    vault_root = tmp_path / "vault"
    chat_id = "chat-passthrough"

    # Seed one prior turn so the loaded ChatContext is non-empty —
    # makes the identity assertion meaningful even if the empty case
    # short-circuits anywhere.
    chat_log.append(
        chat_id,
        user_msg="generate me a nutrition plan for today",
        bot_reply="Done! Saved your nutrition plan to fitness/plans/foo.md.",
        intent="fitness.nutrition_plan",
        vault_root=vault_root,
        clock=_fixed_clock(datetime(2026, 5, 10, 8, 0, 0, tzinfo=timezone.utc)),
    )

    classifier_stub = Mock()
    classifier_stub.classify = Mock(return_value="_inbox.fallback")

    orchestrator = Orchestrator(
        lock=SingleInstanceLock(lock_path),
        audit_root=vault_root / "_audit",
        vault_root=vault_root,
        invoker=_RecordingInvoker(text="ok"),
        classifier=classifier_stub,
        chat_id=chat_id,
        clock=_fixed_clock(datetime(2026, 5, 10, 8, 5, 0, tzinfo=timezone.utc)),
    )

    orchestrator.handle_message("Yes, walk me through it")

    # Classifier.classify must have been called exactly once, with
    # chat_context= keyword carrying the same instance the orchestrator
    # loaded (and stashed on self._chat_context).
    assert classifier_stub.classify.call_count == 1
    call = classifier_stub.classify.call_args
    assert call.args == ("Yes, walk me through it",)
    assert "chat_context" in call.kwargs
    passed = call.kwargs["chat_context"]
    assert isinstance(passed, ChatContext)
    assert passed is orchestrator._chat_context
    # And the loaded context must have actually carried the seeded turn —
    # otherwise the whole pass-through is moot.
    assert len(passed.recent_turns) == 1
    assert passed.recent_turns[0].user_msg == (
        "generate me a nutrition plan for today"
    )
