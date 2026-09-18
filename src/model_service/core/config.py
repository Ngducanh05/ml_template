from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when application configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    model_path: Path
    model_name: str = "iris-demo-v1"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if environ is None else environ

        model_path = values.get("MODEL_PATH", "").strip()
        if not model_path:
            raise ConfigurationError("MODEL_PATH is required")

        model_name = values.get("MODEL_NAME", "iris-demo-v1").strip()
        if not model_name:
            raise ConfigurationError("MODEL_NAME must not be empty")

        log_level = values.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError(
                "LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL"
            )

        return cls(
            model_path=Path(model_path),
            model_name=model_name,
            log_level=log_level,
        )
