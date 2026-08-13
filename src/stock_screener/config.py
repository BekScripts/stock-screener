"""Typed application settings, loaded from the environment.

Settings are read once and cached. Never read `os.environ` directly elsewhere in
the codebase — add a field here instead so the value is typed, validated at
startup, and documented in `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    """Runtime configuration for the application.

    Values come from environment variables, falling back to a local `.env` file.
    Field names map to upper-case env vars, e.g. `log_level` -> `LOG_LEVEL`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    environment: Environment = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = Field(
        default=False,
        description="Emit machine-readable JSON logs. Enable outside local dev.",
    )

    @property
    def is_production(self) -> bool:
        """Whether the app is running in the production environment."""
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings.

    Returns:
        The process-wide `Settings` instance, constructed on first call.
    """
    return Settings()
