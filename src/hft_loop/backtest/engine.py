"""Tick/trade backtest engine with fee, slippage, and latency lag."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import polars as pl

from hft_loop.backtest.metrics import compute_metrics
from hft_loop.backtest.types import BacktestMetrics, BacktestResult, TradeRecord
from hft_loop.latency import apply_latency_correction
from hft_loop.logging_setup import get_logger
from hft_loop.schema import EXCHANGE_TS, PRICE, SYMBOL
from strategies.base import Strategy, StrategyContext

log = get_logger("hft_loop.backtest")


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    fee_bps: float = 1.0
    slippage_bps: float = 0.5
    latency_ms: float = 0.0
    latency_jitter_ms: float = 0.0
    position_notional: float = 10_000.0  # $ notional per |signal|=1
    device: str = "cpu"
    apply_latency: bool = True


def _to_utc_us(df: pl.DataFrame, col: str) -> pl.DataFrame:
    dtype = df.schema[col]
    if isinstance(dtype, pl.Datetime):
        expr = pl.col(col)
        if dtype.time_zone is None:
            expr = expr.dt.replace_time_zone("UTC")
        else:
            expr = expr.dt.convert_time_zone("UTC")
        return df.with_columns(expr.cast(pl.Datetime("us", "UTC")).alias(col))
    return df.with_columns(
        pl.col(col).str.to_datetime(time_zone="UTC").cast(pl.Datetime("us", "UTC"))
    )


def _prepare_market(df: pl.DataFrame, cfg: BacktestConfig) -> pl.DataFrame:
    if EXCHANGE_TS not in df.columns or PRICE not in df.columns:
        raise ValueError(f"Market data requires '{EXCHANGE_TS}' and '{PRICE}' columns")
    out = _to_utc_us(df, EXCHANGE_TS).sort(EXCHANGE_TS)
    out = out.with_columns(pl.col(PRICE).cast(pl.Float64))
    if cfg.apply_latency and (cfg.latency_ms != 0.0 or cfg.latency_jitter_ms > 0):
        out = apply_latency_correction(
            out,
            latency_ms=cfg.latency_ms,
            jitter_ms=cfg.latency_jitter_ms,
            ts_col=EXCHANGE_TS,
            out_col="effective_ts",
        )
    else:
        out = out.with_columns(pl.col(EXCHANGE_TS).alias("effective_ts"))
    return out


def _simulate(
    signaled: pl.DataFrame,
    cfg: BacktestConfig,
) -> tuple[pl.DataFrame, list[TradeRecord]]:
    """Walk bars: signal decided on exchange_ts, fill on first effective_ts lag.

    Simplified model:
    - decision at row i uses signal[i]
    - execution price is price of first row j where effective_ts[j] >= exchange_ts[i] + 0
      (latency already baked into effective_ts shift of the series used for fills)

    Practical approach used here:
    - target position from signal
    - fills occur on next bar after signal change (1-bar lag) to avoid look-ahead,
      plus optional extra lag_rows inferred from latency_ms / median dt
    """
    n = signaled.height
    if n < 3:
        raise ValueError("Need at least 3 rows to backtest")

    prices = signaled.get_column(PRICE).to_numpy().astype(np.float64)
    signals = signaled.get_column("signal").to_numpy().astype(np.float64)
    ts_list = signaled.get_column(EXCHANGE_TS).to_list()
    eff = signaled.get_column("effective_ts").to_numpy()

    # Infer bar spacing (seconds) from exchange_ts
    ts_us = signaled.select(pl.col(EXCHANGE_TS).dt.epoch("us")).to_series().to_numpy()
    dts = np.diff(ts_us.astype(np.float64))
    median_dt_us = float(np.median(dts[dts > 0])) if np.any(dts > 0) else 1_000_000.0
    lag_rows = 1
    if cfg.latency_ms > 0 and median_dt_us > 0:
        lag_rows = max(1, int(round((cfg.latency_ms * 1000.0) / median_dt_us)))

    fee_rate = (cfg.fee_bps + cfg.slippage_bps) / 10_000.0
    notional = cfg.position_notional

    equity = np.empty(n, dtype=np.float64)
    equity[0] = cfg.initial_capital
    pos = np.zeros(n, dtype=np.float64)
    realized = np.zeros(n, dtype=np.float64)

    trades: list[TradeRecord] = []
    cur_pos = 0.0
    entry_px = 0.0
    entry_i = 0
    trade_id = 0
    cash_pnl = 0.0

    for i in range(n):
        # Position updates with lag to model latency / next-touch fill
        target_i = i - lag_rows
        target = float(signals[target_i]) if target_i >= 0 else 0.0
        # clamp
        if target > 1:
            target = 1.0
        elif target < -1:
            target = -1.0

        if target != cur_pos:
            px = prices[i]
            # Close existing
            if cur_pos != 0.0:
                raw = cur_pos * (px - entry_px) / entry_px * notional
                fees = abs(cur_pos) * notional * fee_rate
                pnl = raw - fees
                cash_pnl += pnl
                trade_id += 1
                side = "long" if cur_pos > 0 else "short"
                trades.append(
                    TradeRecord(
                        trade_id=trade_id,
                        entry_ts=str(ts_list[entry_i]),
                        exit_ts=str(ts_list[i]),
                        side=side,
                        entry_price=float(entry_px),
                        exit_price=float(px),
                        qty=abs(cur_pos) * notional / entry_px,
                        pnl=float(pnl),
                        return_pct=float(pnl / notional * 100.0),
                        fees=float(fees),
                    )
                )
            # Open new
            if target != 0.0:
                entry_px = px * (1.0 + fee_rate if target > 0 else 1.0 - fee_rate)
                entry_i = i
            cur_pos = target

        # Mark-to-market equity
        mtm = 0.0
        if cur_pos != 0.0 and entry_px != 0.0:
            mtm = cur_pos * (prices[i] - entry_px) / entry_px * notional
        equity[i] = cfg.initial_capital + cash_pnl + mtm
        pos[i] = cur_pos
        realized[i] = cash_pnl

    # Force flatten at end for clean trade accounting
    if cur_pos != 0.0:
        i = n - 1
        px = prices[i]
        raw = cur_pos * (px - entry_px) / entry_px * notional
        fees = abs(cur_pos) * notional * fee_rate
        pnl = raw - fees
        cash_pnl += pnl
        trade_id += 1
        trades.append(
            TradeRecord(
                trade_id=trade_id,
                entry_ts=str(ts_list[entry_i]),
                exit_ts=str(ts_list[i]),
                side="long" if cur_pos > 0 else "short",
                entry_price=float(entry_px),
                exit_price=float(px),
                qty=abs(cur_pos) * notional / entry_px,
                pnl=float(pnl),
                return_pct=float(pnl / notional * 100.0),
                fees=float(fees),
            )
        )
        equity[i] = cfg.initial_capital + cash_pnl
        pos[i] = 0.0
        realized[i] = cash_pnl

    curve = signaled.with_columns(
        pl.Series("position", pos),
        pl.Series("equity", equity),
        pl.Series("realized_pnl", realized),
        pl.lit(lag_rows).alias("exec_lag_rows"),
    )
    _ = eff  # latency already reflected via lag_rows / effective_ts column retained
    return curve, trades


def run_backtest(
    df: pl.DataFrame,
    strategy: Strategy,
    *,
    cfg: BacktestConfig,
    run_id: str,
    data_path: str,
    strategy_name: str,
    strategy_params: dict[str, Any],
    run_dir: str,
) -> tuple[BacktestResult, pl.DataFrame, list[TradeRecord]]:
    market = _prepare_market(df, cfg)
    symbol = None
    if SYMBOL in market.columns and market.height:
        symbol = str(market.get_column(SYMBOL)[0])

    ctx = StrategyContext(symbol=symbol, device=cfg.device, params=strategy_params)
    log.info(
        "backtest_signals_start",
        strategy=strategy_name,
        rows=market.height,
        device=cfg.device,
        latency_ms=cfg.latency_ms,
    )
    signaled = strategy.generate_signals(market, ctx)
    if "signal" not in signaled.columns:
        raise ValueError("Strategy must produce a 'signal' column")
    if signaled.height != market.height:
        raise ValueError("Strategy changed row count; must be 1:1 with market data")

    curve, trades = _simulate(signaled, cfg)

    ts_us = curve.select(pl.col(EXCHANGE_TS).dt.epoch("us")).to_series().to_numpy()
    dts = np.diff(ts_us.astype(np.float64))
    bar_seconds = float(np.median(dts[dts > 0]) / 1e6) if np.any(dts > 0) else 1.0

    metrics: BacktestMetrics = compute_metrics(
        equity=curve.get_column("equity"),
        trades=trades,
        initial_capital=cfg.initial_capital,
        position=curve.get_column("position"),
        bar_seconds=bar_seconds,
    )

    result = BacktestResult(
        ok=True,
        run_id=run_id,
        strategy=strategy_name,
        strategy_params=strategy_params,
        data_path=data_path,
        symbol=symbol,
        device=cfg.device,
        latency_ms=cfg.latency_ms,
        latency_jitter_ms=cfg.latency_jitter_ms,
        fee_bps=cfg.fee_bps,
        slippage_bps=cfg.slippage_bps,
        metrics=metrics,
        n_bars=curve.height,
        run_dir=run_dir,
        artifacts={},
    )
    log.info(
        "backtest_done",
        run_id=run_id,
        n_trades=metrics.n_trades,
        sharpe=metrics.sharpe,
        total_pnl=metrics.total_pnl,
        max_dd_pct=metrics.max_drawdown_pct,
    )
    return result, curve, trades


def new_run_id(prefix: str = "exp") -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
