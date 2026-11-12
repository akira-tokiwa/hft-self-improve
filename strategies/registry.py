"""Strategy name → class registry and YAML config loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from strategies.base import Strategy
from strategies.dummy_mean_reversion import DummyMeanReversion
from strategies.dummy_buy_hold import DummyBuyHold

_REGISTRY: dict[str, type] = {
    DummyMeanReversion.name: DummyMeanReversion,
    DummyBuyHold.name: DummyBuyHold,
    # aliases
    "dummy": DummyMeanReversion,
}


def list_strategies() -> list[str]:
    return sorted({k for k in _REGISTRY if not k.startswith("_")})


def get_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    key = name.strip().lower()
    if key not in _REGISTRY:
        known = ", ".join(list_strategies())
        raise ValueError(f"Unknown strategy '{name}'. Known: {known}")
    cls = _REGISTRY[key]
    return cls(params or {})  # type: ignore[call-arg]


def load_strategy_config(path: Path) -> dict[str, Any]:
    """Load YAML like: strategy: name\\nparams: {...}"""
    if not path.is_file():
        raise FileNotFoundError(f"Strategy config not found: {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "strategy" not in data and "name" not in data:
        raise ValueError(f"Strategy config must include 'strategy' or 'name': {path}")
    return {
        "strategy": data.get("strategy") or data.get("name"),
        "params": data.get("params") or {},
        "raw": data,
    }
