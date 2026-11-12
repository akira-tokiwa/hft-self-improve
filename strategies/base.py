"""Strategy interface — keep implementations swappable."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import polars as pl


@dataclass
class StrategyContext:
    """Runtime knobs passed into strategies (device, symbol, etc.)."""

    symbol: str | None = None
    device: str = "cpu"  # cpu | cuda (optional; strategies may ignore)
    params: dict[str, Any] = field(default_factory=dict)


class Strategy(Protocol):
    """Produce a per-row target position in [-1, 0, 1] (or continuous)."""

    name: str

    def generate_signals(self, df: pl.DataFrame, ctx: StrategyContext) -> pl.DataFrame:
        """Return df with at least column `signal` (float) aligned row-wise.

        Optional columns: `signal_reason` (str).
        Must preserve row order and length.
        """
        ...
