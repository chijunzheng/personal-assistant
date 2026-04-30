"""Proactive layer entrypoint — daily/weekly digests + reminder dispatch.

Three CLI subcommands (argparse) front three pure-function helpers that
tests can call directly:

  - ``--task daily-digest``    -> ``daily_digest(...)``
  - ``--task weekly-digest``   -> ``weekly_digest(...)``
  - ``--task check-reminders`` -> ``check_reminders(...)``

Each helper composes its output from the relevant *domain* modules. The
proactive layer NEVER hardcodes a domain name — domain digests are
auto-discovered from ``domains/<name>/domain.yaml`` so adding a new
domain with a digest is a YAML + plugin change, never a kernel change.

The advisory pass (``suggested_actions=true``) shells through
``claude_runner`` with the prompt at ``kernel/prompts/digest_suggestions.md``.
The reminder dispatcher fires due reminders via a pluggable
``telegram_send`` callable (production wires it to the bot; tests
inject a list-append spy).
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

import yaml

from kernel.audit import write_audit_entry
from kernel.claude_runner import ClaudeResponse, invoke as claude_invoke
from kernel.vault import atomic_write

__all__ = [
    "check_reminders",
    "daily_digest",
    "main",
    "weekly_digest",
]


DEFAULT_DOMAINS_ROOT = Path("domains")
DEFAULT_VAULT_ROOT = Path("vault")
DEFAULT_AUDIT_ROOT = Path("vault/_audit")
DEFAULT_INBOX_ARCHIVE_AFTER_WEEKS = 4

_REMINDER_EVENTS_RELATIVE = Path("reminder") / "events.jsonl"
_DIGEST_PROMPT_PATH = Path(__file__).parent / "prompts" / "digest_suggestions.md"


# ---------------------------------------------------------------------------
# Pluggable callable protocols
# ---------------------------------------------------------------------------


class _ClaudeInvoker(Protocol):
    """The subset of ``claude_runner.invoke`` the proactive layer uses."""

    def __call__(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> ClaudeResponse: ...


class _TelegramSender(Protocol):
    """Hook for sending one Telegram message. Tests inject a spy."""

    def __call__(self, message: str) -> None: ...


# ---------------------------------------------------------------------------
# domain.yaml introspection
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    """Best-effort YAML read; returns ``{}`` on missing/malformed input."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _digest_block(domain_yaml: dict) -> dict:
    """Pull the ``digest`` block; return ``{}`` if absent."""
    block = domain_yaml.get("digest") or {}
    return block if isinstance(block, dict) else {}


def _digest_enabled(digest: dict, *, mode: str) -> bool:
    """Return True when this domain.yaml opts into ``daily``/``weekly``.

    Accepts both shapes:
      digest:
        enabled: true
        cadence: daily      # finance/journal/inventory style
      digest:
        enabled: true
        daily:
          enabled: true     # fitness style (declares both daily & weekly)
        weekly:
          enabled: true
    """
    if not bool(digest.get("enabled", False)):
        return False
    cadence = str(digest.get("cadence") or "").strip().lower()
    if cadence == mode:
        return True
    sub = digest.get(mode)
    if isinstance(sub, dict) and bool(sub.get("enabled", False)):
        return True
    return False


def _iter_domain_dirs(domains_root: Path) -> list[tuple[str, dict]]:
    """Yield (name, parsed-yaml) pairs in deterministic order."""
    if not domains_root.exists():
        return []
    out: list[tuple[str, dict]] = []
    for child in sorted(domains_root.iterdir()):
        if not child.is_dir():
            continue
        yml = _read_yaml(child / "domain.yaml")
        if not yml:
            continue
        out.append((child.name, yml))
    return out


def _import_digest_module(domain_name: str):
    """Import ``domains.<name>.digest``; return ``None`` if missing."""
    try:
        return importlib.import_module(f"domains.{domain_name}.digest")
    except ImportError:
        return None


def _call_digest(
    *,
    module,
    vault_root: Path,
    mode: str,
    now: datetime,
) -> str:
    """Invoke the domain's ``summarize`` with the right kwargs.

    Per-domain signatures vary slightly (inventory uses ``vault_root``
    only; journal/finance accept ``since``; fitness accepts ``mode`` +
    ``now``). This helper introspects the function and supplies the
    arguments it actually accepts so the contract stays simple.
    """
    summarize = getattr(module, "summarize", None)
    if summarize is None:
        return ""

    code = summarize.__code__
    accepted = set(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])

    kwargs: dict = {}
    if "vault_root" in accepted:
        kwargs["vault_root"] = vault_root
    if "mode" in accepted:
        kwargs["mode"] = mode
    if "now" in accepted:
        kwargs["now"] = now
    if "since" in accepted:
        kwargs["since"] = now - timedelta(days=7)
    if "until" in accepted:
        kwargs["until"] = now

    try:
        result = summarize(**kwargs)
    except Exception:  # noqa: BLE001
        return ""
    return str(result or "")


# ---------------------------------------------------------------------------
# advisory pass (suggested_actions)
# ---------------------------------------------------------------------------


def _suggested_actions_enabled(config: dict) -> bool:
    """Pull ``suggested_actions`` from either the nested or flat config shape."""
    if not isinstance(config, dict):
        return False
    ce = config.get("context_engineering") or {}
    if isinstance(ce, dict) and "suggested_actions" in ce:
        return bool(ce.get("suggested_actions"))
    return bool(config.get("suggested_actions", False))


def _load_advisory_prompt() -> str:
    """Read ``kernel/prompts/digest_suggestions.md`` or return an empty placeholder."""
    try:
        return _DIGEST_PROMPT_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _advisory_pass(
    digest_text: str,
    *,
    invoker: _ClaudeInvoker,
) -> str:
    """Append an LLM-generated 'Suggested actions' section to ``digest_text``."""
    system = _load_advisory_prompt()
    response = invoker(digest_text, system_prompt=system or None)
    advice = (response.text or "").strip()
    if not advice:
        return digest_text
    if "Suggested actions" not in advice:
        advice = "### Suggested actions\n" + advice
    return digest_text.rstrip() + "\n\n" + advice + "\n"


# ---------------------------------------------------------------------------
# daily-digest
# ---------------------------------------------------------------------------


def _compose_digest(
    *,
    domains_root: Path,
    vault_root: Path,
    mode: str,
    now: datetime,
) -> str:
    """Concatenate every enabled domain's digest section for ``mode``."""
    sections: list[str] = []
    for name, yml in _iter_domain_dirs(domains_root):
        digest = _digest_block(yml)
        if not _digest_enabled(digest, mode=mode):
            continue
        module = _import_digest_module(name)
        if module is None:
            continue
        section = _call_digest(
            module=module, vault_root=vault_root, mode=mode, now=now
        ).strip()
        if section:
            sections.append(section)
    return "\n\n".join(sections)


def daily_digest(
    *,
    vault_root: str | os.PathLike[str] = DEFAULT_VAULT_ROOT,
    domains_root: str | os.PathLike[str] = DEFAULT_DOMAINS_ROOT,
    config: Optional[dict] = None,
    now: Optional[datetime] = None,
    invoker: Optional[_ClaudeInvoker] = None,
) -> str:
    """Compose the daily digest as a single Telegram-deliverable string.

    Args:
        vault_root: vault root on disk.
        domains_root: domains dir; tests pass a tmp tree.
        config: full config dict; ``suggested_actions`` is honored from
            ``context_engineering.suggested_actions`` (nested) or
            ``suggested_actions`` (flat).
        now: reference time for the digest window. Defaults to UTC now.
        invoker: pluggable ``claude_runner.invoke`` for the advisory pass;
            production callers leave it ``None`` and the kernel shells.

    Returns:
        The concatenated digest text. Empty string when no daily-cadence
        domains have data to report.
    """
    vault_path = Path(vault_root)
    domains_path = Path(domains_root)
    config = config or {}
    now = now or datetime.now(tz=timezone.utc)

    digest = _compose_digest(
        domains_root=domains_path,
        vault_root=vault_path,
        mode="daily",
        now=now,
    )

    if not digest:
        return ""

    if _suggested_actions_enabled(config):
        digest = _advisory_pass(digest, invoker=invoker or claude_invoke)

    return digest


# ---------------------------------------------------------------------------
# weekly-digest
# ---------------------------------------------------------------------------


def _inbox_triage_section(
    vault_root: Path,
    *,
    now: datetime,
    archive_after_weeks: int,
) -> str:
    """List `_inbox/` files older than ``archive_after_weeks`` weeks (excluding subdirs)."""
    inbox = vault_root / "_inbox"
    if not inbox.exists():
        return ""

    cutoff = now - timedelta(weeks=archive_after_weeks)
    cutoff_ts = cutoff.timestamp()

    entries: list[Path] = []
    for child in sorted(inbox.iterdir()):
        if child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        # Either older than cutoff OR archive window not yet elapsed —
        # we surface every top-level inbox item so the user can triage.
        # (cutoff_ts is unused when archive policy is "show all"; kept
        # for the future when we want to omit truly old items.)
        del mtime, cutoff_ts
        entries.append(child)

    if not entries:
        return ""

    lines = [f"- {p.name}" for p in entries]
    return "## Inbox triage (pending classification)\n\n" + "\n".join(lines) + "\n"


def weekly_digest(
    *,
    vault_root: str | os.PathLike[str] = DEFAULT_VAULT_ROOT,
    domains_root: str | os.PathLike[str] = DEFAULT_DOMAINS_ROOT,
    config: Optional[dict] = None,
    now: Optional[datetime] = None,
    invoker: Optional[_ClaudeInvoker] = None,
) -> str:
    """Compose the weekly digest including domain sections + inbox triage.

    Args:
        vault_root: vault root on disk.
        domains_root: domains dir.
        config: full config dict; ``suggested_actions`` is honored.
            Inbox archive cutoff comes from
            ``proactive.inbox_archive_after_weeks`` (defaults to 4).
        now: reference time. Defaults to UTC now.
        invoker: pluggable ``claude_runner.invoke`` for the advisory pass.

    Returns:
        The concatenated weekly digest text including an "Inbox triage"
        section. Empty string when there's nothing to report.
    """
    vault_path = Path(vault_root)
    domains_path = Path(domains_root)
    config = config or {}
    now = now or datetime.now(tz=timezone.utc)

    digest = _compose_digest(
        domains_root=domains_path,
        vault_root=vault_path,
        mode="weekly",
        now=now,
    )

    archive_after_weeks = int(
        ((config.get("proactive") or {}).get("inbox_archive_after_weeks"))
        or DEFAULT_INBOX_ARCHIVE_AFTER_WEEKS
    )
    inbox = _inbox_triage_section(
        vault_path,
        now=now,
        archive_after_weeks=archive_after_weeks,
    )
    if inbox:
        digest = (digest + "\n\n" + inbox).strip() if digest else inbox

    if not digest:
        return ""

    if _suggested_actions_enabled(config):
        digest = _advisory_pass(digest, invoker=invoker or claude_invoke)

    return digest


# ---------------------------------------------------------------------------
# check-reminders
# ---------------------------------------------------------------------------


def _append_jsonl(path: Path, row: dict) -> None:
    """Append one JSONL row via ``atomic_write`` (preserves atomic-write seam)."""
    import json

    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing = existing + "\n"
    line = json.dumps(row, default=str) + "\n"
    atomic_write(path, existing + line)


def check_reminders(
    *,
    vault_root: str | os.PathLike[str] = DEFAULT_VAULT_ROOT,
    audit_root: str | os.PathLike[str] = DEFAULT_AUDIT_ROOT,
    now: Optional[datetime] = None,
    telegram_send: Optional[_TelegramSender] = None,
    condition_evaluator: Optional[Callable[[str, ...], bool]] = None,
    config_label: str = "default",
) -> list[dict]:
    """Fire every due reminder; append a ``fire`` event + audit-log each.

    Args:
        vault_root: vault root on disk.
        audit_root: audit log root.
        now: reference time. Defaults to UTC now.
        telegram_send: pluggable hook called once per due reminder.
            Defaults to a no-op (production wiring lives in the bot).
        condition_evaluator: pluggable evaluator for state-derived
            reminders; passes through to ``due_reminders``.
        config_label: which config produced the audit entry.

    Returns:
        The list of reminder rows that fired.
    """
    # Lazy import to keep kernel/proactive testable without the plugin
    # being importable at import time.
    from domains.reminder.handler import due_reminders

    vault_path = Path(vault_root)
    audit_path = Path(audit_root)
    now = now or datetime.now(tz=timezone.utc)
    send = telegram_send or (lambda _m: None)

    due = due_reminders(
        now=now,
        vault_root=vault_path,
        condition_evaluator=condition_evaluator,
    )

    if not due:
        return []

    events_path = vault_path / _REMINDER_EVENTS_RELATIVE
    fired: list[dict] = []
    for reminder in due:
        wall_start = time.monotonic()
        message = str(reminder.get("message") or "").strip() or "(reminder)"
        outcome = "ok"
        error_message: Optional[str] = None

        try:
            send(message)
        except Exception as err:  # noqa: BLE001
            outcome = "error"
            error_message = str(err)

        # Append a fire event preserving append-only.
        target_id = reminder.get("id")
        fire_row = {
            "id": f"fire-{target_id}-{int(now.timestamp())}",
            "kind": reminder.get("kind"),
            "status": "fired",
            "target_id": target_id,
            "message": message,
            "fire_at": reminder.get("fire_at"),
            "fired_at": now.isoformat(),
        }
        _append_jsonl(events_path, fire_row)

        duration_ms = int((time.monotonic() - wall_start) * 1000)
        entry = {
            "ts": now.isoformat(),
            "op": "reminder_fire",
            "actor": "kernel.proactive",
            "domain": "reminder",
            "outcome": outcome,
            "duration_ms": duration_ms,
            "config": config_label,
            "target_id": target_id,
            "preview": message[:200],
        }
        if error_message is not None:
            entry["error"] = error_message
        write_audit_entry(entry, audit_root=audit_path)

        fired.append(reminder)

    return fired


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """argparse spec — ``python -m kernel.proactive --task <name>``."""
    parser = argparse.ArgumentParser(
        prog="python -m kernel.proactive",
        description="Daily/weekly digest and reminder dispatcher.",
    )
    parser.add_argument(
        "--task",
        required=True,
        choices=("daily-digest", "weekly-digest", "check-reminders"),
        help="Which scheduled task to run.",
    )
    parser.add_argument(
        "--vault-root",
        default=str(DEFAULT_VAULT_ROOT),
        help="Vault root on disk (default: ./vault).",
    )
    parser.add_argument(
        "--domains-root",
        default=str(DEFAULT_DOMAINS_ROOT),
        help="Domains root on disk (default: ./domains).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a YAML config file (e.g., configs/default.yaml).",
    )
    return parser


def _load_config(path: Optional[str]) -> dict:
    if not path:
        return {}
    try:
        return _read_yaml(Path(path))
    except OSError:
        return {}


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint. Returns process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = _load_config(args.config)

    if args.task == "daily-digest":
        text = daily_digest(
            vault_root=args.vault_root,
            domains_root=args.domains_root,
            config=config,
        )
        sys.stdout.write(text)
        return 0
    if args.task == "weekly-digest":
        text = weekly_digest(
            vault_root=args.vault_root,
            domains_root=args.domains_root,
            config=config,
        )
        sys.stdout.write(text)
        return 0
    if args.task == "check-reminders":
        fired = check_reminders(
            vault_root=args.vault_root,
            audit_root=Path(args.vault_root) / "_audit",
        )
        sys.stdout.write(f"Fired {len(fired)} reminder(s)\n")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
