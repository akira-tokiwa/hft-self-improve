#!/usr/bin/env python3
"""Download market data from Tardis / Databento (or --dry-run synthetic).

Examples:
  python scripts/download_data.py --dry-run --days 1
  python scripts/download_data.py --provider tardis --symbol BTC-PERPETUAL \\
      --start 2024-01-01 --end 2024-01-02
  python scripts/download_data.py --provider databento --venue GLBX.MDP3 \\
      --dataset trades --symbol ESH4 --start 2024-01-01 --end 2024-01-02
  python scripts/download_data.py --dry-run --as-live --days 1
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401

from hft_loop.config import load_settings, resolve_path
from hft_loop.env import load_env
from hft_loop.logging_setup import get_logger, setup_logging
from hft_loop.providers import DownloadRequest, get_provider


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download market data (Tardis / Databento)")
    p.add_argument("--config", default=None, help="Path to settings.yaml")
    p.add_argument("--provider", default=None, help="tardis | databento | synthetic")
    p.add_argument("--symbol", default=None)
    p.add_argument("--venue", default=None, help="Exchange / Databento dataset id")
    p.add_argument("--dataset", default=None, help="trades | book_snapshot_5 | mbp-1 | ...")
    p.add_argument("--start", default=None, help="UTC start date/datetime ISO")
    p.add_argument("--end", default=None, help="UTC end date/datetime ISO")
    p.add_argument("--days", type=int, default=None, help="If set, end=now and start=now-days")
    p.add_argument(
        "--format",
        dest="output_format",
        choices=("parquet", "raw"),
        default=None,
        help="parquet (default) writes data/parquet; raw keeps provider payload under data/raw",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call provider APIs; write synthetic Parquet instead",
    )
    p.add_argument(
        "--as-live",
        action="store_true",
        help="With --dry-run, also stamp local_ts and write under data/live/",
    )
    p.add_argument("--n-rows", type=int, default=None, help="Synthetic row count")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--json-out",
        default=None,
        help="Write machine-readable result JSON (for Grok Bot) to this path",
    )
    return p.parse_args()


def _resolve_window(
    args: argparse.Namespace,
    data_cfg: dict,
) -> tuple[str, str]:
    if args.start and args.end:
        return args.start, args.end
    days = args.days if args.days is not None else int(data_cfg.get("default_days", 1))
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")


def main() -> int:
    args = _parse_args()
    load_env()
    settings = load_settings(args.config)
    setup_logging(settings)
    log = get_logger("download_data")

    data_cfg = settings.get("data", {})
    provider_name = (args.provider or data_cfg.get("default_provider") or "tardis").lower()
    symbol = args.symbol or data_cfg.get("default_symbol")
    venue = args.venue or data_cfg.get("default_venue")
    dataset = args.dataset or data_cfg.get("default_dataset", "trades")
    output_format = args.output_format or data_cfg.get("default_output_format", "parquet")
    compression = data_cfg.get("compression", "zstd")

    if not symbol or not venue:
        log.error("missing_symbol_or_venue", symbol=symbol, venue=venue)
        return 2

    start, end = _resolve_window(args, data_cfg)
    dry_run = bool(args.dry_run or data_cfg.get("force_dry_run", False))

    raw_dir = resolve_path(settings, "data_raw")
    parquet_dir = resolve_path(settings, "data_parquet")
    live_dir = resolve_path(settings, "data_live")
    raw_dir.mkdir(parents=True, exist_ok=True)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    live_dir.mkdir(parents=True, exist_ok=True)

    extra = {
        "n_rows": args.n_rows or int(data_cfg.get("dry_run_rows", 500)),
        "seed": args.seed,
        "as_live": args.as_live,
        "live_dir": str(live_dir),
        "latency_ms_mean": float(
            settings.get("backtest", {}).get("latency_ms", 4.0)
        ),
        "latency_ms_jitter": float(data_cfg.get("dry_run_jitter_ms", 1.5)),
        "db_dataset": data_cfg.get("databento_dataset"),
        "stype_in": data_cfg.get("databento_stype_in", "raw_symbol"),
    }

    request = DownloadRequest(
        provider=provider_name,
        symbol=symbol,
        venue=venue,
        dataset=dataset,
        start=start,
        end=end,
        dry_run=dry_run,
        output_format=output_format,
        extra=extra,
    )

    log.info(
        "download_start",
        provider=provider_name,
        symbol=symbol,
        venue=venue,
        dataset=dataset,
        start=start,
        end=end,
        dry_run=dry_run,
        output_format=output_format,
        as_live=args.as_live,
    )

    try:
        provider = get_provider(provider_name, dry_run=dry_run)
        if hasattr(provider, "base_url") and data_cfg.get("tardis_base_url"):
            provider.base_url = str(data_cfg["tardis_base_url"]).rstrip("/")  # type: ignore[attr-defined]
        result = provider.download(
            request,
            raw_dir=raw_dir,
            parquet_dir=parquet_dir,
            compression=compression,
        )
    except (EnvironmentError, PermissionError, FileNotFoundError, ValueError, ImportError) as exc:
        log.error("download_failed", error=str(exc), error_type=type(exc).__name__)
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("download_unexpected_error", error=str(exc))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1

    payload = {
        "ok": True,
        "provider": result.provider,
        "symbol": result.symbol,
        "rows": result.rows,
        "dry_run": result.dry_run,
        "paths": [str(p) for p in result.paths],
        "meta": result.meta,
    }
    log.info("download_done", **{k: v for k, v in payload.items() if k != "meta"}, meta=result.meta)

    if args.json_out:
        out = args.json_out
        from pathlib import Path

        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        log.info("json_out_written", path=str(path))
    else:
        # Always print a single JSON line summary for Bot parsers on stdout last.
        print(json.dumps(payload))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
