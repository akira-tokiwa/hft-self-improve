"""Load secrets from environment / .env (never from YAML)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from hft_loop.config import PROJECT_ROOT

_ENV_LOADED = False


def load_env(dotenv_path: Path | None = None) -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    path = dotenv_path or (PROJECT_ROOT / ".env")
    load_dotenv(path, override=False)
    _ENV_LOADED = True


def require_env(name: str) -> str:
    load_env()
    value = os.getenv(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"Environment variable {name} is required. "
            f"Set it in the shell or in {PROJECT_ROOT / '.env'}."
        )
    return value


def get_env(name: str, default: str | None = None) -> str | None:
    load_env()
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()
