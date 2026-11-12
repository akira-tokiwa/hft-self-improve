#!/usr/bin/env python3
"""Compare live vs historical feeds, estimate latency/jitter, apply to history.

Examples:
  python scripts/latency_correction.py \\
      --live data/live/.../trades_....parquet \\
      --historical data/parquet/.../trades_....parquet

  python scripts/latency_correction.py --auto-latest --apply-jitter
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401

from hft_loop.config import load_settings, resolve_path
from hft_loop.env import load_env
from hft_loop.io import latest_parquet
from hft_loop.latency import apply_latency_correction, run_latency_pipeline
from hft_loop.logging_setup import get_logger, setup_logging
from hft_loop.io import read_parquet, write_parquet


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Latency estimation and correction")
    p.add_argument("--config", default=None)
    p.add_argument("--live", default=None, help="Live parquet file/dir (has local_ts)")
    p.add_argument("--historical", default=None, help="Historical parquet file/dir")
    p.add_argument(
        "--auto-latest",
        action="store_true",
        help="Use latest parquet under data/live and data/parquet",
    )
    p.add_argument(
        "--latency-ms",
        type=float,
        default=None,
        help="Override applied mean latency (skip using estimated mean)",
    )
    p.add_argument(
        "--apply-jitter",
        action="store_true",
        help="When applying, also add N(0, std) jitter from live estimate",
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="experiments/<run-id>/ output folder name",
    )
    p.add_argument(
        "--apply-only",
        action="store_true",
        help="Only apply --latency-ms to --historical (no live compare)",
    )
    p.add_argument("--json-out", default=None, help="Write result JSON path")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    load_env()
    settings = load_settings(args.config)
    setup_logging(settings)
    log = get_logger("latency_correction")

    data_cfg = settings.get("data", {})
    compression = data_cfg.get("compression", "zstd")
    exp_root = resolve_path(settings, "experiments")
    run_id = args.run_id or (
        f"latency_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    out_dir = exp_root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.apply_only:
            if args.historical is None and not args.auto_latest:
                raise ValueError("--apply-only requires --historical or --auto-latest")
            if args.latency_ms is None:
                raise ValueError("--apply-only requires --latency-ms")
            hist_path = (
                Path(args.historical)
                if args.historical
                else latest_parquet(resolve_path(settings, "data_parquet"))
            )
            hist_df = read_parquet(hist_path)
            corrected = apply_latency_correction(
                hist_df,
                latency_ms=float(args.latency_ms),
                jitter_ms=0.0,
            )
            out_pq = out_dir / "historical_latency_corrected.parquet"
            write_parquet(corrected, out_pq, compression=compression)
            payload = {
                "ok": True,
                "mode": "apply_only",
                "run_id": run_id,
                "hist_path": str(hist_path),
                "applied_latency_ms": float(args.latency_ms),
                "corrected_path": str(out_pq),
            }
        else:
            live_path: Path
            hist_path: Path
            if args.auto_latest:
                live_path = latest_parquet(resolve_path(settings, "data_live"))
                hist_path = latest_parquet(resolve_path(settings, "data_parquet"))
            else:
                if not args.live or not args.historical:
                    raise ValueError("Provide --live and --historical, or --auto-latest")
                live_path = Path(args.live)
                hist_path = Path(args.historical)

            result = run_latency_pipeline(
                live_path=live_path,
                hist_path=hist_path,
                out_dir=out_dir,
                latency_ms_override=args.latency_ms,
                apply_jitter=args.apply_jitter,
                compression=compression,
            )
            payload = {"ok": True, "mode": "compare_and_apply", "run_id": run_id, **result}

        metrics_path = out_dir / "metrics.json"
        metrics_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        summary_path = out_dir / "summary.md"
        stats = payload.get("stats") or {}
        summary_path.write_text(
            f"# {run_id}\n\n"
            f"- mode: `{payload.get('mode')}`\n"
            f"- applied_latency_ms: `{payload.get('applied_latency_ms')}`\n"
            f"- mean_ms: `{stats.get('mean_ms')}`\n"
            f"- std_ms (jitter): `{stats.get('std_ms')}`\n"
            f"- n_samples: `{stats.get('n_samples')}`\n"
            f"- corrected: `{payload.get('corrected_path')}`\n",
            encoding="utf-8",
        )
        log.info("latency_run_saved", run_id=run_id, out_dir=str(out_dir))

    except (FileNotFoundError, ValueError, KeyError) as exc:
        log.error("latency_failed", error=str(exc), error_type=type(exc).__name__)
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("latency_unexpected_error", error=str(exc))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
        )
    print(json.dumps(payload, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
