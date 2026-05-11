"""Per-turn chat-context value type — verbatim turns + lossy summary.

The orchestrator loads ``ChatContext`` once at the top of every turn and
hands the same instance to every dispatch helper. That single value is
the answer to *"what has the user been talking about?"* and pairs the
two complementary signals:

  - ``recent_turns`` — verbatim user/bot exchanges (default last 5),
    chronological newest-last. This is what lets *"yes, walk me through
    it"* resolve to its antecedent. Sourced from ``kernel.chat_log``.
  - ``session_summary`` — the rolling, lossy summary body of the
    active session (one bullet per dispatch). This is what lets longer
    sessions retain higher-level continuity that a 5-turn window can't
    carry. Sourced from ``kernel.session.Session.summary``.

Per the ADR at ``docs/decisions/2026-05-10-chat-context-and-capabilities.md``,
this is a frozen value type with no I/O — handlers read fields off the
instance the orchestrator passed them; they do not re-load. The
load-once-per-turn discipline keeps prompt-token cost bounded and makes
classifier/handler input deterministic for a given turn boundary.

The companion helper ``compose_recent_preamble`` renders the verbatim
turns into the canonical prompt block (``## Recent conversation`` /
``User: ...`` / ``You: ...``) used by every handler that folds chat
context into its LLM call. Handlers should call this helper rather than
re-implementing the format so the rendering stays consistent across the
echo path, the journal-query path, and the fitness-plan path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

from kernel.chat_log import ChatTurn

__all__ = ["ChatContext", "compose_recent_preamble"]


@dataclass(frozen=True)
class ChatContext:
    """Immutable per-turn view of "what the user has been talking about".

    Attributes:
        recent_turns: Verbatim user/bot exchanges, chronological newest-
            last. Empty for the first turn of a brand-new chat. The
            orchestrator currently loads up to 5 turns; callers MUST NOT
            assume a fixed length.
        session_summary: The rolling summary body from the active
            session file. Empty string when the session is fresh / has
            no notes yet. Sourced from
            ``kernel.session.Session.summary``.
    """

    recent_turns: Tuple[ChatTurn, ...] = field(default_factory=tuple)
    session_summary: str = ""


def compose_recent_preamble(recent_turns: Tuple[ChatTurn, ...] | list) -> str:
    """Render the verbatim turns as a "Recent conversation" prompt block.

    Returns the empty string when there are no prior turns so callers
    can unconditionally prepend the result to their per-turn prompt
    without an extra blank-line guard. The format mirrors the echo
    path's ``_compose_echo_prompt`` so handlers that fold this in
    surface the same preamble shape to the LLM.
    """
    if not recent_turns:
        return ""

    lines = ["## Recent conversation"]
    for turn in recent_turns:
        lines.append(f"User: {turn.user_msg}")
        lines.append(f"You: {turn.bot_reply}")
    lines.append("")  # blank separator before whatever the caller appends
    return "\n".join(lines)
