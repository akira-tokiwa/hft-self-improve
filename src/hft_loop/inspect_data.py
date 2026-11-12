"""Inspect Parquet market-data files (stats / missingness)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from hft_loop.io import df_summary, latest_parquet, list_parquet_files, read_parquet
from hft_loop.logging_setup import get_logger
from hft_loop.schema import EXCHANGE_TS, PRICE, SIZE

log = get_logger("hft_loop.inspect")


def inspect_frame(df: pl.DataFrame, *, sample_rows: int = 5) -> dict[str, Any]:
    summary = df_summary(df)
    nulls = {c: int(df.get_column(c).null_count()) for c in df.columns}
    null_frac = {c: (nulls[c] / df.height if df.height else 0.0) for c in df.columns}

    numeric_stats: dict[str, Any] = {}
    for col, dtype in df.schema.items():
        if dtype.is_numeric():
            s = df.select(
                pl.col(col).mean().alias("mean"),
                pl.col(col).std().alias("std"),
                pl.col(col).min().alias("min"),
                pl.col(col).max().alias("max"),
            ).row(0, named=True)
            numeric_stats[col] = {k: (float(v) if v is not None else None) for k, v in s.items()}

    time_range = None
    if EXCHANGE_TS in df.columns and df.height:
        try:
            tmin = df.select(pl.col(EXCHANGE_TS).min()).item()
            tmax = df.select(pl.col(EXCHANGE_TS).max()).item()
            time_range = {"min": str(tmin), "max": str(tmax)}
        except Exception as exc:  # noqa: BLE001
            time_range = {"error": str(exc)}

    # Duplicate / monotonic checks for exchange_ts when present
    quality: dict[str, Any] = {}
    if EXCHANGE_TS in df.columns and df.height:
        n_dup = df.height - df.select(EXCHANGE_TS).unique().height
        quality["duplicate_exchange_ts"] = int(n_dup)
        quality["is_sorted_exchange_ts"] = bool(
            df.select((pl.col(EXCHANGE_TS).diff().drop_nulls() >= 0).all()).item()
        )

    for required in (PRICE, SIZE):
        if required in nulls:
            quality[f"null_{required}"] = nulls[required]

    head = df.head(sample_rows).to_dicts()
    # stringify non-json-friendly
    for row in head:
        for k, v in list(row.items()):
            if hasattr(v, "isoformat"):
                row[k] = v.isoformat()

    report = {
        **summary,
        "null_counts": nulls,
        "null_fraction": null_frac,
        "numeric_stats": numeric_stats,
        "exchange_ts_range": time_range,
        "quality": quality,
        "head": head,
    }
    log.info(
        "inspect_done",
        rows=summary["rows"],
        cols=summary["cols"],
        null_total=sum(nulls.values()),
    )
    return report


def inspect_path(path: Path, *, sample_rows: int = 5) -> dict[str, Any]:
    df = read_parquet(path)
    report = inspect_frame(df, sample_rows=sample_rows)
    report["path"] = str(path)
    return report


def inspect_latest(root: Path, *, sample_rows: int = 5) -> dict[str, Any]:
    path = latest_parquet(root)
    report = inspect_path(path, sample_rows=sample_rows)
    report["search_root"] = str(root)
    report["parquet_files_found"] = len(list_parquet_files(root))
    return report


def report_to_json(report: dict[str, Any], path: Path | None = None) -> str:
    text = json.dumps(report, indent=2, default=str) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        log.info("inspect_report_written", path=str(path))
    return text
