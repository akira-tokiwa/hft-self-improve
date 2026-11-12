"""Load and resolve project settings from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.yaml"


def load_settings(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    with path.open(encoding="utf-8") as f:
        settings = yaml.safe_load(f) or {}
    settings["_config_path"] = str(path)
    settings["_project_root"] = str(PROJECT_ROOT)
    return settings


def resolve_path(settings: dict[str, Any], key: str) -> Path:
    """Resolve a paths.* key relative to project root."""
    rel = settings.get("paths", {}).get(key)
    if not rel:
        raise KeyError(f"paths.{key} is not set in settings")
    root = Path(settings["_project_root"])
    return (root / rel).resolve()
