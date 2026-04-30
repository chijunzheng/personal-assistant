"""Tests for ``kernel.classifier`` — YAML-driven intent registry + LLM dispatch.

The classifier reads every ``domains/*/domain.yaml`` at startup, builds a flat
intent registry, and (per-turn) calls ``claude_runner`` with the classifier
prompt to map a free-form message onto one of those intents. Anything the LLM
returns that isn't a registered intent collapses to ``_inbox.fallback``.

These tests verify external behavior — discovery, dispatch, fallback — and
mock the LLM call so they don't shell out to ``claude -p``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kernel.classifier import (
    FALLBACK_INTENT,
    Classifier,
    discover_intents,
)


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


def test_discover_intents_reads_every_domain_yaml(tmp_path: Path) -> None:
    """Every ``domains/*/domain.yaml`` contributes its declared intents."""
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "journal", ["journal.capture", "journal.query"])
    _write_domain(domains_root, "finance", ["finance.transaction", "finance.query"])

    intents = discover_intents(domains_root)

    assert "journal.capture" in intents
    assert "journal.query" in intents
    assert "finance.transaction" in intents
    assert "finance.query" in intents


def test_discover_intents_skips_directories_without_domain_yaml(tmp_path: Path) -> None:
    """A subdirectory without a ``domain.yaml`` is silently ignored, not an error."""
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "journal", ["journal.capture"])
    (domains_root / "scratch").mkdir(parents=True, exist_ok=True)  # no domain.yaml

    intents = discover_intents(domains_root)

    assert intents == ("journal.capture",)


def test_classify_returns_known_intent_when_llm_emits_one(tmp_path: Path) -> None:
    """A clear-intent reply from the LLM passes straight through as the classification."""
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "journal", ["journal.capture", "journal.query"])

    def fake_invoker(prompt, *, system_prompt=None):
        from kernel.claude_runner import ClaudeResponse

        return ClaudeResponse(text="journal.capture", tokens_in=10, tokens_out=2, raw={})

    classifier = Classifier(domains_root=domains_root, invoker=fake_invoker)

    intent = classifier.classify("interesting idea about consciousness")

    assert intent == "journal.capture"


def test_classify_falls_back_to_inbox_for_unknown_label(tmp_path: Path) -> None:
    """An LLM reply that doesn't match any registered intent collapses to the fallback."""
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "journal", ["journal.capture"])

    def fake_invoker(prompt, *, system_prompt=None):
        from kernel.claude_runner import ClaudeResponse

        return ClaudeResponse(text="something.weird", tokens_in=10, tokens_out=2, raw={})

    classifier = Classifier(domains_root=domains_root, invoker=fake_invoker)

    intent = classifier.classify("???")

    assert intent == FALLBACK_INTENT
    assert FALLBACK_INTENT == "_inbox.fallback"


def test_classify_strips_whitespace_and_codeblock_fencing(tmp_path: Path) -> None:
    """LLMs sometimes wrap output in fences/whitespace; the classifier tolerates that."""
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "journal", ["journal.capture"])

    def fake_invoker(prompt, *, system_prompt=None):
        from kernel.claude_runner import ClaudeResponse

        return ClaudeResponse(
            text="```\njournal.capture\n```\n",
            tokens_in=10,
            tokens_out=2,
            raw={},
        )

    classifier = Classifier(domains_root=domains_root, invoker=fake_invoker)

    intent = classifier.classify("a thought")

    assert intent == "journal.capture"


def test_classifier_includes_intent_list_in_prompt(tmp_path: Path) -> None:
    """The classifier composes the LLM prompt with the discovered intent list."""
    domains_root = tmp_path / "domains"
    _write_domain(domains_root, "journal", ["journal.capture"])
    _write_domain(domains_root, "finance", ["finance.transaction"])

    captured: dict[str, str] = {}

    def fake_invoker(prompt, *, system_prompt=None):
        from kernel.claude_runner import ClaudeResponse

        captured["prompt"] = prompt
        return ClaudeResponse(text="journal.capture", tokens_in=1, tokens_out=1, raw={})

    classifier = Classifier(domains_root=domains_root, invoker=fake_invoker)
    classifier.classify("a thought")

    assert "journal.capture" in captured["prompt"]
    assert "finance.transaction" in captured["prompt"]
    assert "a thought" in captured["prompt"]
