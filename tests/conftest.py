"""Shared pytest fixtures for the root application test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from stock_screener.config import Settings

if TYPE_CHECKING:
    from pathlib import Path

_SETTINGS_ENV_VARS = ("ENVIRONMENT", "LOG_LEVEL", "LOG_JSON")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate every test from the developer's real environment.

    Moves to an empty working directory so no `.env` file is picked up, and
    clears the variables `Settings` reads. Tests that want a value set it
    explicitly with `monkeypatch.setenv`.
    """
    monkeypatch.chdir(tmp_path)
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    """Return settings pinned to the test environment."""
    return Settings(environment="test")
