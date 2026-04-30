"""Tests for ``kernel.vault`` — verify external behavior, not implementation."""

from __future__ import annotations

from pathlib import Path

from kernel.vault import atomic_write


def test_atomic_write_creates_file_with_exact_content(tmp_path: Path) -> None:
    """A successful atomic_write places exactly the requested bytes at the path."""
    target = tmp_path / "note.md"
    payload = "hello world\n"

    atomic_write(target, payload)

    assert target.read_text(encoding="utf-8") == payload


def test_atomic_write_leaves_no_partial_tmp_when_replace_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """If the rename step fails, the function does not leave ``.tmp`` debris.

    Drive Desktop will happily sync any file it sees, including a
    half-written ``.tmp``. Cleaning up on failure is part of the contract.
    """
    import os as os_module

    target = tmp_path / "note.md"

    def boom(_src, _dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os_module, "replace", boom)

    try:
        atomic_write(target, "doomed content")
    except OSError:
        pass

    assert not target.exists()
    assert not (tmp_path / "note.md.tmp").exists()


def test_atomic_write_is_idempotent_on_identical_content(tmp_path: Path) -> None:
    """Writing the same content twice yields identical bytes — no append, no corruption."""
    target = tmp_path / "note.md"
    payload = "stable content\n"

    atomic_write(target, payload)
    atomic_write(target, payload)

    assert target.read_text(encoding="utf-8") == payload


def test_atomic_write_creates_missing_parent_directories(tmp_path: Path) -> None:
    """Callers should not have to mkdir before writing; vault paths nest."""
    target = tmp_path / "vault" / "journal" / "2026-04-29.md"

    atomic_write(target, "today\n")

    assert target.read_text(encoding="utf-8") == "today\n"
