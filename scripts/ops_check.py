#!/usr/bin/env python3
"""Smoke-check local environment, paths, and config."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from hft_loop.config import load_settings, resolve_path
from hft_loop.logging_setup import get_logger, setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="Ops / environment check")
    parser.add_argument("--config", default=None, help="Path to settings.yaml")
    args = parser.parse_args()

    settings = load_settings(args.config)
    setup_logging(settings)
    log = get_logger("ops_check")

    ok = True
    for key in ("data_raw", "data_parquet", "data_live", "experiments", "knowledge"):
        path = resolve_path(settings, key)
        path.mkdir(parents=True, exist_ok=True)
        exists = path.is_dir()
        log.info("path_check", key=key, path=str(path), ok=exists)
        ok = ok and exists

    py = sys.version_info
    log.info(
        "python_version",
        version=f"{py.major}.{py.minor}.{py.micro}",
        ok=py >= (3, 11),
    )
    if py < (3, 11):
        ok = False

    try:
        import polars  # noqa: F401
        import pyarrow  # noqa: F401
        import structlog  # noqa: F401
        import yaml  # noqa: F401

        log.info("deps_ok", packages=["polars", "pyarrow", "structlog", "pyyaml"])
    except ImportError as exc:
        log.error("deps_missing", error=str(exc))
        ok = False

    allowed = settings.get("grok", {}).get("allowed_scripts", [])
    for rel in allowed:
        p = Path(settings["_project_root"]) / rel
        present = p.is_file()
        log.info("script_whitelist", script=rel, ok=present)
        ok = ok and present

    log.info("ops_check_done", status="ok" if ok else "fail")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
