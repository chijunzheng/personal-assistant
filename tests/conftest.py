"""Shared pytest fixtures for the personal-assistant kernel test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    """A per-test flock path that won't collide with the production lock."""
    return tmp_path / "personal-assistant.lock"
