"""User-facing capabilities preamble, derived from registered domains.

Reads every ``domains/*/domain.yaml`` and renders a compact markdown bullet
list summarizing what the assistant can actually do for the user. The
result is substituted into ``kernel/prompts/system.md`` at orchestrator
construction time so the LLM answers *"what can you do?"* from the live
domain set rather than from whatever the system prompt happened to say
when it was last edited.

The cardinal rule: this module is **data-driven**. New domains register
by dropping a ``domain.yaml`` with two new optional fields
(``description_user_facing`` and ``intent_examples``); the kernel never
hardcodes a domain name. Domains that omit the fields degrade silently.

See ``docs/decisions/2026-05-10-chat-context-and-capabilities.md`` for
the design contract.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import yaml

__all__ = ["build_capabilities_preamble"]


# Cap on rendered examples per domain — keeps the preamble under the
# loose ~500-token budget called out in the ADR even with five domains.
_MAX_EXAMPLES_PER_DOMAIN = 3

# Header line shown above the bullet list when at least one domain
# rendered. Empty preambles render the stub-form instead.
_PREAMBLE_HEADER = "You can help Jason with:"

# Stub returned when no domain produced a usable description (empty
# domains tree, or every domain missed ``description_user_facing``).
# Keeps the placeholder substitution non-empty so downstream prompts
# don't end up with awkward blank sections.
_EMPTY_STUB = "You can help Jason with anything he brings up — ask for clarification when needed."


def _humanize_name(name: str) -> str:
    """Turn ``fitness`` into ``Fitness``; preserve internal casing for any future
    multi-word names.
    """
    if not name:
        return name
    return name[0].upper() + name[1:]


def _iter_domain_yamls(root: Path) -> Iterable[tuple[str, dict]]:
    """Yield ``(domain_dir_name, parsed_yaml)`` for each readable domain.yaml.

    Skips:
      - non-directories
      - directories starting with ``_`` or ``__`` (kernel-managed scratch /
        Python package markers)
      - directories without a ``domain.yaml``
      - YAML files that fail to parse (logged-implicitly via skipping;
        we degrade rather than crash so a malformed plugin can't take
        the whole bot down)
    """
    if not root.exists() or not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("_") or name.startswith("__"):
            continue
        domain_yaml = child / "domain.yaml"
        if not domain_yaml.exists():
            continue
        try:
            payload = yaml.safe_load(domain_yaml.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        yield name, payload


def _flatten_examples(intent_examples: object) -> list[str]:
    """Flatten the ``intent_examples`` mapping into a single ordered list.

    The schema is ``{intent_name: [example, ...]}``. We pull examples in
    intent-declaration order (i.e., dict insertion order) so the rendered
    preamble feels written-by-hand rather than alphabetized. Non-string
    examples are dropped silently.
    """
    if not isinstance(intent_examples, dict):
        return []
    flat: list[str] = []
    for examples in intent_examples.values():
        if not isinstance(examples, list):
            continue
        for ex in examples:
            if isinstance(ex, str) and ex.strip():
                flat.append(ex.strip())
    return flat


def _render_domain_bullet(
    *, display_name: str, description: str, examples: list[str]
) -> str:
    """Format a single domain into its two-line bullet."""
    head = f"- **{display_name}** -- {description}"
    if not examples:
        return head
    rendered = " * ".join(f'"{ex}"' for ex in examples[:_MAX_EXAMPLES_PER_DOMAIN])
    return f"{head}\n  Examples: {rendered}"


def build_capabilities_preamble(domains_root: str | os.PathLike[str]) -> str:
    """Build a user-facing capabilities preamble suitable for system-prompt injection.

    Args:
        domains_root: filesystem path to the ``domains/`` directory.

    Returns:
        A markdown bullet list naming each domain that supplied a
        ``description_user_facing`` field, with up to three example
        phrasings each. If no domain qualifies, returns a short stub
        rather than an empty string so the substituted system prompt
        always has a non-empty capabilities slot.
    """
    root = Path(domains_root)
    bullets: list[str] = []
    for dir_name, payload in _iter_domain_yamls(root):
        description = payload.get("description_user_facing")
        if not isinstance(description, str) or not description.strip():
            # Degrade silently: a domain without a user-facing description
            # is excluded from the preamble. This keeps the kernel from
            # leaking developer-language descriptions into chat replies.
            continue
        display_name = _humanize_name(payload.get("name") or dir_name)
        examples = _flatten_examples(payload.get("intent_examples"))
        bullets.append(
            _render_domain_bullet(
                display_name=display_name,
                description=description.strip(),
                examples=examples,
            )
        )

    if not bullets:
        return _EMPTY_STUB

    return _PREAMBLE_HEADER + "\n\n" + "\n\n".join(bullets)
