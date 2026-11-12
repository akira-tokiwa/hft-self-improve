#!/usr/bin/env python3
"""Inspect latest (or given) Parquet: schema, nulls, basic stats.

Examples:
  python scripts/inspect_data.py
  python scripts/inspect_data.py --path data/parquet/tardis/...
  python scripts/inspect_data.py --root data/live --json-out /tmp/inspect.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

from hft_loop.config import load_settings, resolve_path
from hft_loop.env import load_env
from hft_loop.inspect_data import inspect_latest, inspect_path, report_to_json
from hft_loop.logging_setup import get_logger, setup_logging


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inspect Parquet market data")
    p.add_argument("--config", default=None)
    p.add_argument("--path", default=None, help="Specific parquet file or directory")
    p.add_argument(
        "--root",
        default=None,
        choices=("parquet", "live", "raw"),
        help="Search latest under data/<root> (default: parquet)",
    )
    p.add_argument("--sample-rows", type=int, default=5)
    p.add_argument("--json-out", default=None)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    load_env()
    settings = load_settings(args.config)
    setup_logging(settings)
    log = get_logger("inspect_data")

    try:
        if args.path:
            report = inspect_path(Path(args.path), sample_rows=args.sample_rows)
        else:
            root_key = {
                None: "data_parquet",
                "parquet": "data_parquet",
                "live": "data_live",
                "raw": "data_raw",
            }[args.root]
            root = resolve_path(settings, root_key)
            report = inspect_latest(root, sample_rows=args.sample_rows)
    except FileNotFoundError as exc:
        log.error("inspect_failed", error=str(exc))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("inspect_unexpected_error", error=str(exc))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1

    report["ok"] = True
    if args.json_out:
        report_to_json(report, Path(args.json_out))
    print(json.dumps(report, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
