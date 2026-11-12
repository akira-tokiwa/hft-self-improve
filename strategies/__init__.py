"""Pluggable trading strategies for the backtest engine."""

from __future__ import annotations

from strategies.base import Strategy, StrategyContext
from strategies.registry import get_strategy, list_strategies, load_strategy_config

__all__ = [
    "Strategy",
    "StrategyContext",
    "get_strategy",
    "list_strategies",
    "load_strategy_config",
]
