"""Tests for ``domains.journal.handler.read`` — query-side of the journal plugin.

The read path is what serves "what did I think about X?" turns. It accepts
the user's query, the active intent, and a ``ContextBundle`` already
gathered by ``kernel.retrieval``. It must:

  - Reach an LLM via an injected invoker (so tests don't shell out)
  - Pass the bundle's snippets into the LLM prompt so the agent has grounded context
  - Cite the consulted notes (paths or filenames) in the reply

The handler stays log-silent: the kernel writes the audit entry after read
returns, per CLAUDE.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from domains.journal.handler import read
from kernel.claude_runner import ClaudeResponse
from kernel.retrieval import ContextBundle, Snippet


def _bundle_with(*paths: Path) -> ContextBundle:
    """Build a bundle whose snippets/paths reflect the provided files."""
    snippets = tuple(
        Snippet(source=str(p), text=p.read_text(encoding="utf-8"))
        for p in paths
    )
    return ContextBundle(snippets=snippets, paths=tuple(paths))


def _seed_journal(vault_root: Path, name: str, body: str) -> Path:
    """Drop a journal note for the bundle to reference."""
    target = vault_root / "journal" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


class _RecordingInvoker:
    """Stub ``claude_runner.invoke`` that captures the prompt the kernel sent."""

    def __init__(self, reply: str = "answer", tokens_in: int = 5, tokens_out: int = 3) -> None:
        self.reply = reply
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.last_prompt: Optional[str] = None
        self.last_system_prompt: Optional[str] = None

    def __call__(self, prompt: str, *, system_prompt: Optional[str] = None) -> ClaudeResponse:
        self.last_prompt = prompt
        self.last_system_prompt = system_prompt
        return ClaudeResponse(
            text=self.reply,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            raw={},
        )


def test_read_returns_response_text_from_invoker(tmp_path: Path) -> None:
    """``read`` returns the LLM's reply text verbatim."""
    note = _seed_journal(
        tmp_path / "vault",
        "2026-04-10-coffee.md",
        "I drank too much coffee this week",
    )
    bundle = _bundle_with(note)
    invoker = _RecordingInvoker(reply="You wrote about coffee on 2026-04-10.")

    result = read(
        intent="journal.query",
        query="what did I write about coffee?",
        context_bundle=bundle,
        invoker=invoker,
    )

    assert result.reply_text == "You wrote about coffee on 2026-04-10."


def test_read_includes_snippet_text_in_llm_prompt(tmp_path: Path) -> None:
    """The bundle's snippet text reaches the LLM via the prompt."""
    note = _seed_journal(
        tmp_path / "vault",
        "2026-04-10-tiered-memory.md",
        "tiered memory architectures are interesting",
    )
    bundle = _bundle_with(note)
    invoker = _RecordingInvoker()

    read(
        intent="journal.query",
        query="memory",
        context_bundle=bundle,
        invoker=invoker,
    )

    assert invoker.last_prompt is not None
    assert "tiered memory architectures are interesting" in invoker.last_prompt


def test_read_includes_consulted_paths_in_llm_prompt(tmp_path: Path) -> None:
    """The agent prompt names the source files so the reply can cite them."""
    note = _seed_journal(
        tmp_path / "vault",
        "2026-04-11-elections.md",
        "thoughts on the upcoming election",
    )
    bundle = _bundle_with(note)
    invoker = _RecordingInvoker()

    read(
        intent="journal.query",
        query="elections",
        context_bundle=bundle,
        invoker=invoker,
    )

    # The prompt must surface the path so the LLM can cite it back.
    assert "2026-04-11-elections.md" in invoker.last_prompt


def test_read_surfaces_consulted_paths_on_result(tmp_path: Path) -> None:
    """The result exposes the same path list the bundle carried, for audit."""
    note_a = _seed_journal(tmp_path / "vault", "2026-04-12-a.md", "alpha")
    note_b = _seed_journal(tmp_path / "vault", "2026-04-12-b.md", "beta")
    bundle = _bundle_with(note_a, note_b)
    invoker = _RecordingInvoker()

    result = read(
        intent="journal.query",
        query="anything",
        context_bundle=bundle,
        invoker=invoker,
    )

    assert tuple(result.consulted_paths) == (note_a, note_b)


def test_read_with_empty_bundle_still_returns_text(tmp_path: Path) -> None:
    """Empty bundle is not an error — agent gets a 'no notes found' style prompt."""
    bundle = ContextBundle(snippets=(), paths=())
    invoker = _RecordingInvoker(reply="No matching notes were found.")

    result = read(
        intent="journal.query",
        query="anything at all",
        context_bundle=bundle,
        invoker=invoker,
    )

    assert result.reply_text == "No matching notes were found."
    assert result.consulted_paths == ()


def test_read_includes_user_query_in_prompt(tmp_path: Path) -> None:
    """The user's question is passed through so the LLM has both context + question."""
    note = _seed_journal(tmp_path / "vault", "2026-04-13-ml.md", "ML thoughts")
    bundle = _bundle_with(note)
    invoker = _RecordingInvoker()

    read(
        intent="journal.query",
        query="what did I think about ML?",
        context_bundle=bundle,
        invoker=invoker,
    )

    assert "what did I think about ML?" in invoker.last_prompt


def test_read_passes_journal_system_prompt_to_invoker(tmp_path: Path) -> None:
    """The dedicated ``read_journal.md`` prompt is loaded and passed to the runner."""
    note = _seed_journal(tmp_path / "vault", "2026-04-15-x.md", "stuff")
    bundle = _bundle_with(note)
    invoker = _RecordingInvoker()

    read(
        intent="journal.query",
        query="anything",
        context_bundle=bundle,
        invoker=invoker,
    )

    # The prompt mentions "cite" — that token comes from read_journal.md only.
    assert invoker.last_system_prompt is not None
    assert "cite" in invoker.last_system_prompt.lower()


def test_read_token_telemetry_is_surfaced(tmp_path: Path) -> None:
    """``tokens_in`` / ``tokens_out`` from the runner reach the result for auditing."""
    note = _seed_journal(tmp_path / "vault", "2026-04-14-x.md", "whatever")
    bundle = _bundle_with(note)
    invoker = _RecordingInvoker(tokens_in=42, tokens_out=11)

    result = read(
        intent="journal.query",
        query="x",
        context_bundle=bundle,
        invoker=invoker,
    )

    assert result.tokens_in == 42
    assert result.tokens_out == 11
