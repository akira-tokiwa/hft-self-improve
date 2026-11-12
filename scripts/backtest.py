#!/usr/bin/env python3
"""Run a strategy backtest for the self-improvement loop.

Examples:
  python scripts/backtest.py --strategy dummy_mean_reversion --data data/parquet
  python scripts/backtest.py --strategy-config config/strategies/dummy_mean_reversion.yaml \\
      --data data/parquet/tardis/BTC-PERPETUAL --latency-ms 5
  python scripts/backtest.py --strategy dummy_buy_hold --data data/parquet --device cpu
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from hft_loop.backtest.engine import BacktestConfig, new_run_id, run_backtest
from hft_loop.backtest.writer import append_experiments_index, write_experiment
from hft_loop.config import load_settings, resolve_path
from hft_loop.env import load_env
from hft_loop.io import latest_parquet, read_parquet
from hft_loop.logging_setup import get_logger, setup_logging
from strategies import get_strategy, list_strategies, load_strategy_config


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HFT backtest runner")
    p.add_argument("--config", default=None, help="Global settings.yaml")
    p.add_argument(
        "--strategy",
        default=None,
        help=f"Strategy name. Known: {', '.join(list_strategies())}",
    )
    p.add_argument(
        "--strategy-config",
        default=None,
        help="YAML with strategy + params (overrides --strategy name if set)",
    )
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override strategy param (repeatable), e.g. --param window=30",
    )
    p.add_argument(
        "--data",
        default=None,
        help="Parquet file or directory (default: latest under data/parquet)",
    )
    p.add_argument("--run-id", default=None)
    p.add_argument("--latency-ms", type=float, default=None)
    p.add_argument("--latency-jitter-ms", type=float, default=0.0)
    p.add_argument("--no-latency", action="store_true", help="Disable latency correction")
    p.add_argument("--fee-bps", type=float, default=None)
    p.add_argument("--slippage-bps", type=float, default=None)
    p.add_argument("--initial-capital", type=float, default=None)
    p.add_argument("--position-notional", type=float, default=None)
    p.add_argument(
        "--device",
        default="cpu",
        choices=("cpu", "cuda"),
        help="Optional device hint for strategies (dummy ignores GPU)",
    )
    p.add_argument("--json-out", default=None, help="Write summary JSON copy")
    p.add_argument("--list-strategies", action="store_true")
    return p.parse_args()


def _parse_params(items: list[str]) -> dict:
    out: dict = {}
    for raw in items:
        if "=" not in raw:
            raise ValueError(f"--param must be KEY=VALUE, got: {raw}")
        k, v = raw.split("=", 1)
        k = k.strip()
        v = v.strip()
        # best-effort numeric/bool
        if v.lower() in {"true", "false"}:
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def main() -> int:
    args = _parse_args()
    if args.list_strategies:
        print(json.dumps({"ok": True, "strategies": list_strategies()}))
        return 0

    load_env()
    settings = load_settings(args.config)
    setup_logging(settings)
    log = get_logger("backtest")

    bt_cfg = settings.get("backtest", {})
    data_cfg = settings.get("data", {})
    exp_cfg = settings.get("experiments", {})
    compression = data_cfg.get("compression", "zstd")

    try:
        strategy_name: str
        strategy_params: dict = {}
        strategy_snapshot: dict

        if args.strategy_config:
            loaded = load_strategy_config(Path(args.strategy_config))
            strategy_name = str(loaded["strategy"])
            strategy_params = dict(loaded.get("params") or {})
            strategy_snapshot = loaded
        elif args.strategy:
            strategy_name = args.strategy
            strategy_snapshot = {"strategy": strategy_name, "params": {}}
        else:
            strategy_name = str(bt_cfg.get("default_strategy", "dummy_mean_reversion"))
            strategy_snapshot = {"strategy": strategy_name, "params": {}}

        strategy_params.update(_parse_params(args.param))
        strategy_snapshot["params"] = strategy_params

        if args.data:
            data_path = Path(args.data)
            if not data_path.is_absolute():
                data_path = Path(settings["_project_root"]) / data_path
            if data_path.is_dir():
                data_path = latest_parquet(data_path)
        else:
            data_path = latest_parquet(resolve_path(settings, "data_parquet"))

        df = read_parquet(data_path)
        strategy = get_strategy(strategy_name, strategy_params)

        latency_ms = (
            0.0
            if args.no_latency
            else (
                float(args.latency_ms)
                if args.latency_ms is not None
                else float(bt_cfg.get("latency_ms", 0.0))
            )
        )
        apply_latency = (not args.no_latency) and bool(
            bt_cfg.get("apply_latency_correction", True)
        )

        cfg = BacktestConfig(
            initial_capital=float(
                args.initial_capital
                if args.initial_capital is not None
                else bt_cfg.get("initial_capital", 100_000.0)
            ),
            fee_bps=float(
                args.fee_bps if args.fee_bps is not None else bt_cfg.get("fee_bps", 1.0)
            ),
            slippage_bps=float(
                args.slippage_bps
                if args.slippage_bps is not None
                else bt_cfg.get("slippage_bps", 0.5)
            ),
            latency_ms=latency_ms,
            latency_jitter_ms=float(args.latency_jitter_ms or 0.0),
            position_notional=float(
                args.position_notional
                if args.position_notional is not None
                else bt_cfg.get("position_notional", 10_000.0)
            ),
            device=args.device,
            apply_latency=apply_latency and latency_ms != 0.0,
        )

        run_id = args.run_id or new_run_id(str(exp_cfg.get("id_prefix", "exp")))
        run_dir = resolve_path(settings, "experiments") / run_id

        log.info(
            "backtest_start",
            run_id=run_id,
            strategy=strategy_name,
            data=str(data_path),
            rows=df.height,
            latency_ms=cfg.latency_ms,
            device=cfg.device,
        )

        result, curve, trades = run_backtest(
            df,
            strategy,
            cfg=cfg,
            run_id=run_id,
            data_path=str(data_path),
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            run_dir=str(run_dir),
        )
        result = write_experiment(
            run_dir=run_dir,
            result=result,
            curve=curve,
            trades=trades,
            compression=compression,
            write_summary_md=bool(exp_cfg.get("write_summary_md", True)),
            strategy_config_snapshot=strategy_snapshot,
        )

        index_path = Path(settings["_project_root"]) / "knowledge" / "experiments_index.md"
        append_experiments_index(
            index_path,
            run_id=run_id,
            symbol=result.symbol,
            status="ok",
            pnl=result.metrics.total_pnl,
            notes=f"{strategy_name}; sharpe={result.metrics.sharpe}",
        )

        summary = result.to_summary_dict()
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
            )
        print(json.dumps(summary, default=str))
        return 0

    except (FileNotFoundError, ValueError, KeyError) as exc:
        log.error("backtest_failed", error=str(exc), error_type=type(exc).__name__)
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("backtest_unexpected_error", error=str(exc))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
