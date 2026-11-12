"""Trivial long-biased dummy — useful as a baseline."""

from __future__ import annotations

from typing import Any

import polars as pl

from strategies.base import StrategyContext


class DummyBuyHold:
    name = "dummy_buy_hold"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        p = params or {}
        self.position = float(p.get("position", 1.0))

    def generate_signals(self, df: pl.DataFrame, ctx: StrategyContext) -> pl.DataFrame:
        _ = ctx
        return df.with_columns(
            pl.lit(self.position).alias("signal"),
            pl.lit(self.name).alias("signal_reason"),
        )
