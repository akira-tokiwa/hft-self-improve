"""Dummy z-score mean-reversion strategy (CPU). Easy to replace later."""

from __future__ import annotations

from typing import Any

import polars as pl

from hft_loop.schema import PRICE
from strategies.base import StrategyContext


class DummyMeanReversion:
    name = "dummy_mean_reversion"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        p = params or {}
        self.window = int(p.get("window", 50))
        self.entry_z = float(p.get("entry_z", 1.25))
        self.exit_z = float(p.get("exit_z", 0.25))

    def generate_signals(self, df: pl.DataFrame, ctx: StrategyContext) -> pl.DataFrame:
        if PRICE not in df.columns:
            raise KeyError(f"Strategy requires column '{PRICE}'")
        if ctx.device.startswith("cuda"):
            # Placeholder: real GPU path can move rolling stats to cupy/torch later.
            pass

        w = max(self.window, 2)
        priced = df.with_columns(pl.col(PRICE).cast(pl.Float64).alias("_px"))
        featured = priced.with_columns(
            pl.col("_px").rolling_mean(window_size=w, min_samples=w).alias("_mu"),
            pl.col("_px").rolling_std(window_size=w, min_samples=w).alias("_sd"),
        ).with_columns(
            pl.when(pl.col("_sd") > 0)
            .then((pl.col("_px") - pl.col("_mu")) / pl.col("_sd"))
            .otherwise(0.0)
            .alias("zscore")
        )

        # Hysteresis: enter when |z| > entry_z, flatten when |z| < exit_z
        signals: list[float] = []
        pos = 0.0
        zvals = featured.get_column("zscore").to_list()
        for z in zvals:
            if z is None:
                signals.append(pos)
                continue
            if pos == 0.0:
                if z >= self.entry_z:
                    pos = -1.0  # fade rich
                elif z <= -self.entry_z:
                    pos = 1.0  # fade cheap
            else:
                if abs(z) <= self.exit_z:
                    pos = 0.0
            signals.append(pos)

        return featured.with_columns(
            pl.Series("signal", signals),
            pl.lit(self.name).alias("signal_reason"),
        ).drop(["_px", "_mu", "_sd"])
