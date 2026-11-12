"""Performance metrics from equity curve and closed trades."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
import polars as pl

from hft_loop.backtest.types import BacktestMetrics, TradeRecord


def _safe_div(a: float, b: float) -> float | None:
    if b == 0 or math.isnan(b) or math.isnan(a):
        return None
    return a / b


def compute_metrics(
    *,
    equity: pl.Series,
    trades: Sequence[TradeRecord],
    initial_capital: float,
    position: pl.Series | None = None,
    periods_per_year: float = 365.0 * 24.0 * 60.0,  # minute-ish default; scaled below
    bar_seconds: float | None = None,
) -> BacktestMetrics:
    eq = equity.cast(pl.Float64).drop_nulls().to_numpy()
    if eq.size == 0:
        raise ValueError("Equity series is empty")

    final_equity = float(eq[-1])
    total_pnl = final_equity - initial_capital
    total_return_pct = (total_pnl / initial_capital) * 100.0 if initial_capital else 0.0

    rets = np.diff(eq) / np.maximum(eq[:-1], 1e-12)
    sharpe = None
    sortino = None
    if rets.size >= 2 and np.std(rets, ddof=1) > 0:
        # Annualization: infer from median bar spacing when provided
        ann = periods_per_year
        if bar_seconds and bar_seconds > 0:
            ann = (365.0 * 24.0 * 3600.0) / bar_seconds
        sharpe = float(np.mean(rets) / np.std(rets, ddof=1) * math.sqrt(ann))
        downside = rets[rets < 0]
        if downside.size >= 2 and np.std(downside, ddof=1) > 0:
            sortino = float(np.mean(rets) / np.std(downside, ddof=1) * math.sqrt(ann))

    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    max_dd_abs = float(dd.min()) if dd.size else 0.0
    max_dd_pct = float((dd / np.maximum(peak, 1e-12)).min() * 100.0) if dd.size else 0.0

    n_trades = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    n_wins = len(wins)
    n_losses = len(losses)
    win_rate = _safe_div(float(n_wins), float(n_trades))
    avg_trade = float(np.mean([t.pnl for t in trades])) if trades else None
    gross_profit = float(sum(t.pnl for t in wins)) if wins else 0.0
    gross_loss = float(abs(sum(t.pnl for t in losses))) if losses else 0.0
    profit_factor = _safe_div(gross_profit, gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else None
    )
    if profit_factor == float("inf"):
        profit_factor = None  # keep JSON-friendly; note in exposure instead

    exposure = None
    if position is not None and position.len() > 0:
        exposure = float((position.abs() > 0).mean())

    # Recompute PF with a capped sentinel for Bot readability
    if gross_loss == 0 and gross_profit > 0:
        profit_factor = None

    return BacktestMetrics(
        total_pnl=total_pnl,
        total_return_pct=total_return_pct,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_abs=max_dd_abs,
        n_trades=n_trades,
        n_wins=n_wins,
        n_losses=n_losses,
        win_rate=win_rate,
        avg_trade_pnl=avg_trade,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else (
            None if gross_profit == 0 else 999.0
        ),
        final_equity=final_equity,
        initial_capital=initial_capital,
        exposure_frac=exposure,
    )
