"""Persist structured backtest artifacts under experiments/<run_id>/."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

import polars as pl

from hft_loop.backtest.types import BacktestResult, TradeRecord
from hft_loop.io import write_parquet
from hft_loop.logging_setup import get_logger

log = get_logger("hft_loop.backtest.writer")


def write_experiment(
    *,
    run_dir: Path,
    result: BacktestResult,
    curve: pl.DataFrame,
    trades: Sequence[TradeRecord],
    compression: str = "zstd",
    write_summary_md: bool = True,
    strategy_config_snapshot: dict[str, Any] | None = None,
) -> BacktestResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}

    metrics_path = run_dir / "metrics.json"
    full_path = run_dir / "result.json"
    metrics_path.write_text(
        json.dumps(result.to_summary_dict(), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    full_path.write_text(
        json.dumps(result.to_full_dict(), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    artifacts["metrics_json"] = str(metrics_path)
    artifacts["result_json"] = str(full_path)

    trades_rows = [asdict(t) for t in trades]
    trades_json = run_dir / "trades.json"
    trades_json.write_text(json.dumps(trades_rows, indent=2, default=str) + "\n", encoding="utf-8")
    artifacts["trades_json"] = str(trades_json)

    if trades_rows:
        trades_pq = run_dir / "trades.parquet"
        write_parquet(pl.DataFrame(trades_rows), trades_pq, compression=compression)
        artifacts["trades_parquet"] = str(trades_pq)

    # Keep equity curve lean for Bot/tooling
    keep = [
        c
        for c in (
            "exchange_ts",
            "effective_ts",
            "price",
            "signal",
            "position",
            "equity",
            "realized_pnl",
            "symbol",
        )
        if c in curve.columns
    ]
    curve_pq = run_dir / "equity_curve.parquet"
    write_parquet(curve.select(keep), curve_pq, compression=compression)
    artifacts["equity_curve_parquet"] = str(curve_pq)

    if strategy_config_snapshot is not None:
        cfg_path = run_dir / "strategy_config.json"
        cfg_path.write_text(
            json.dumps(strategy_config_snapshot, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        artifacts["strategy_config_json"] = str(cfg_path)

    if write_summary_md:
        m = result.metrics
        summary = run_dir / "summary.md"
        summary.write_text(
            f"# {result.run_id}\n\n"
            f"- strategy: `{result.strategy}`\n"
            f"- symbol: `{result.symbol}`\n"
            f"- data: `{result.data_path}`\n"
            f"- device: `{result.device}`\n"
            f"- latency_ms: `{result.latency_ms}` (jitter `{result.latency_jitter_ms}`)\n"
            f"- bars: `{result.n_bars}`\n\n"
            f"## Metrics\n\n"
            f"| metric | value |\n|--------|-------|\n"
            f"| total_pnl | {m.total_pnl:.4f} |\n"
            f"| total_return_pct | {m.total_return_pct:.4f} |\n"
            f"| sharpe | {m.sharpe} |\n"
            f"| sortino | {m.sortino} |\n"
            f"| max_drawdown_pct | {m.max_drawdown_pct:.4f} |\n"
            f"| n_trades | {m.n_trades} |\n"
            f"| win_rate | {m.win_rate} |\n"
            f"| profit_factor | {m.profit_factor} |\n"
            f"| final_equity | {m.final_equity:.4f} |\n",
            encoding="utf-8",
        )
        artifacts["summary_md"] = str(summary)

    result.artifacts = artifacts
    # refresh summary json with artifact paths
    metrics_path.write_text(
        json.dumps(result.to_summary_dict(), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    full_path.write_text(
        json.dumps(result.to_full_dict(), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    log.info("experiment_written", run_dir=str(run_dir), artifacts=list(artifacts))
    return result


def append_experiments_index(
    index_path: Path,
    *,
    run_id: str,
    symbol: str | None,
    status: str,
    pnl: float,
    notes: str,
) -> None:
    if not index_path.exists():
        return
    with index_path.open("a", encoding="utf-8") as f:
        f.write(
            f"| {run_id} | {symbol or '-'} | {status} | {pnl:.4f} | {notes} |\n"
        )
