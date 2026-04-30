"""Tests for the launchd plists under ``infra/launchd/``.

These tests verify install-time correctness without actually invoking
``launchctl load`` (which would have system-level side effects). We
check that:

  1. Each plist parses as a valid plist via ``plistlib``.
  2. Each plist's ``Label`` matches its filename stem.
  3. Schedule fields (``StartCalendarInterval`` / ``StartInterval``) match
     the proactive cadences declared in ``configs/default.yaml`` and
     ``kernel/PROACTIVE.md``.
  4. The bot plist sets ``KeepAlive=True`` and ``RunAtLoad=True`` so
     launchd auto-restarts it on crash and starts it at install.
  5. Every plist invokes Python via ``python -m kernel.<module>`` —
     i.e., they don't bake absolute paths to scripts and they exercise
     the same module entrypoints the tests already exercise.
  6. Each plist's ``EnvironmentVariables`` block carries a
     ``TELEGRAM_BOT_TOKEN`` key (the README documents how to populate it).
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_LAUNCHD_DIR = _REPO_ROOT / "infra" / "launchd"

_PLIST_FILENAMES = (
    "com.jasonchi.pa.daily-digest.plist",
    "com.jasonchi.pa.weekly-digest.plist",
    "com.jasonchi.pa.reminder-check.plist",
    "com.jasonchi.pa.conflict-watcher.plist",
    "com.jasonchi.pa.bot.plist",
)


def _load(filename: str) -> dict:
    """Parse a plist into a Python dict."""
    path = _LAUNCHD_DIR / filename
    with path.open("rb") as handle:
        return plistlib.load(handle)


@pytest.mark.parametrize("filename", _PLIST_FILENAMES)
def test_plist_parses_as_valid_plist(filename: str) -> None:
    """Every plist must be syntactically valid XML / plist."""
    parsed = _load(filename)
    assert isinstance(parsed, dict)


@pytest.mark.parametrize("filename", _PLIST_FILENAMES)
def test_plist_label_matches_filename_stem(filename: str) -> None:
    """``Label`` must equal the filename without ``.plist`` — launchd's invariant."""
    parsed = _load(filename)
    expected = filename.removesuffix(".plist")
    assert parsed.get("Label") == expected


@pytest.mark.parametrize("filename", _PLIST_FILENAMES)
def test_plist_invokes_python_module(filename: str) -> None:
    """Each plist must run ``python ... -m kernel.<module>`` (no absolute script paths)."""
    parsed = _load(filename)
    program_arguments = parsed.get("ProgramArguments")
    assert isinstance(program_arguments, list) and program_arguments, (
        f"{filename} must declare ProgramArguments"
    )
    # Some entry in argv must reference the Python interpreter — either
    # directly (``/usr/bin/python3.12``) or via env-shim (``python3.12``
    # following ``/usr/bin/env``). Exact name is install-time tunable.
    assert any("python" in arg.lower() for arg in program_arguments), (
        f"{filename} ProgramArguments lacks a python reference: {program_arguments!r}"
    )
    # ``-m kernel.<module>`` must appear consecutively somewhere after.
    assert "-m" in program_arguments
    m_index = program_arguments.index("-m")
    assert m_index + 1 < len(program_arguments), (
        f"{filename} declared -m without a module argument"
    )
    module_arg = program_arguments[m_index + 1]
    assert module_arg.startswith("kernel."), (
        f"{filename} must invoke a kernel.* module, got {module_arg!r}"
    )


@pytest.mark.parametrize("filename", _PLIST_FILENAMES)
def test_plist_environment_variables_carry_token_key(filename: str) -> None:
    """Every plist exposes ``TELEGRAM_BOT_TOKEN`` (placeholder is fine for templating)."""
    parsed = _load(filename)
    env = parsed.get("EnvironmentVariables")
    assert isinstance(env, dict), (
        f"{filename} must declare EnvironmentVariables dict"
    )
    assert "TELEGRAM_BOT_TOKEN" in env, (
        f"{filename} EnvironmentVariables missing TELEGRAM_BOT_TOKEN"
    )


def test_daily_digest_runs_at_8am() -> None:
    """daily-digest fires once a day at 08:00 via StartCalendarInterval."""
    parsed = _load("com.jasonchi.pa.daily-digest.plist")
    schedule = parsed.get("StartCalendarInterval")
    assert schedule == {"Hour": 8, "Minute": 0}
    # Must invoke the daily-digest task explicitly.
    assert "daily-digest" in parsed["ProgramArguments"]


def test_weekly_digest_runs_sunday_6pm() -> None:
    """weekly-digest fires Sunday (Weekday=0) at 18:00."""
    parsed = _load("com.jasonchi.pa.weekly-digest.plist")
    schedule = parsed.get("StartCalendarInterval")
    assert schedule == {"Weekday": 0, "Hour": 18, "Minute": 0}
    assert "weekly-digest" in parsed["ProgramArguments"]


def test_reminder_check_runs_every_5_minutes() -> None:
    """reminder-check uses StartInterval=300 — matches reminder_check_interval_min=5."""
    parsed = _load("com.jasonchi.pa.reminder-check.plist")
    assert parsed.get("StartInterval") == 300
    assert "check-reminders" in parsed["ProgramArguments"]


def test_conflict_watcher_runs_every_minute() -> None:
    """conflict-watcher uses StartInterval=60 — matches conflict_watcher_interval_min=1."""
    parsed = _load("com.jasonchi.pa.conflict-watcher.plist")
    assert parsed.get("StartInterval") == 60
    # Single-shot scan: we want each launchd invocation to exit, not loop.
    assert "--run-once" in parsed["ProgramArguments"]


def test_bot_plist_keeps_alive_and_runs_at_load() -> None:
    """The polling-loop bot must auto-restart on crash and start at install."""
    parsed = _load("com.jasonchi.pa.bot.plist")
    assert parsed.get("KeepAlive") is True
    assert parsed.get("RunAtLoad") is True
    # No schedule keys — long-running process, not periodic.
    assert "StartInterval" not in parsed
    assert "StartCalendarInterval" not in parsed
    # Bot module is the polling-loop entrypoint.
    assert "kernel.telegram_bridge" in parsed["ProgramArguments"]


@pytest.mark.parametrize("filename", _PLIST_FILENAMES)
def test_plist_declares_log_paths(filename: str) -> None:
    """Each plist routes stdout + stderr to a log file for post-mortem inspection."""
    parsed = _load(filename)
    assert "StandardOutPath" in parsed, f"{filename} missing StandardOutPath"
    assert "StandardErrorPath" in parsed, f"{filename} missing StandardErrorPath"


@pytest.mark.parametrize("filename", _PLIST_FILENAMES)
def test_plist_declares_working_directory(filename: str) -> None:
    """Each plist must set WorkingDirectory so relative imports/paths resolve."""
    parsed = _load(filename)
    assert "WorkingDirectory" in parsed, f"{filename} missing WorkingDirectory"


def test_install_readme_exists_and_documents_launchctl() -> None:
    """The install README walks the user through load / list / unload."""
    readme = _LAUNCHD_DIR / "README.md"
    assert readme.exists(), "infra/launchd/README.md is required"
    content = readme.read_text(encoding="utf-8")
    for needle in ("launchctl load", "launchctl list", "launchctl unload", "TELEGRAM_BOT_TOKEN"):
        assert needle in content, f"README.md missing reference to {needle!r}"


def test_plist_labels_are_unique() -> None:
    """No two plists may share a launchd Label — they'd collide on load."""
    labels = [_load(f)["Label"] for f in _PLIST_FILENAMES]
    assert len(set(labels)) == len(labels), f"duplicate labels: {labels}"
