"""Estimate feed latency from live vs historical, and apply corrections."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import polars as pl

from hft_loop.io import read_parquet, write_parquet
from hft_loop.logging_setup import get_logger
from hft_loop.schema import EXCHANGE_TS, LOCAL_TS, SEQUENCE, SYMBOL

log = get_logger("hft_loop.latency")


@dataclass
class LatencyStats:
    n_samples: int
    mean_ms: float
    std_ms: float  # jitter (sample std)
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    method: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_datetime(df: pl.DataFrame, col: str) -> pl.DataFrame:
    if col not in df.columns:
        raise KeyError(f"Required column missing: {col}")
    dtype = df.schema[col]
    if isinstance(dtype, pl.Datetime):
        # Normalize to UTC microsecond datetime
        expr = pl.col(col)
        if dtype.time_zone is None:
            expr = expr.dt.replace_time_zone("UTC")
        else:
            expr = expr.dt.convert_time_zone("UTC")
        return df.with_columns(expr.cast(pl.Datetime("us", "UTC")).alias(col))
    if dtype in (pl.Int64, pl.Int32, pl.UInt64, pl.UInt32, pl.Float64, pl.Float32):
        max_v = df.select(pl.col(col).max()).item()
        if max_v is None:
            raise ValueError(f"Column {col} is empty")
        if max_v > 1e17:
            unit = "ns"
        elif max_v > 1e14:
            unit = "us"
        elif max_v > 1e11:
            unit = "ms"
        else:
            unit = "s"
        return df.with_columns(
            pl.from_epoch(pl.col(col).cast(pl.Int64), time_unit=unit)
            .dt.replace_time_zone("UTC")
            .cast(pl.Datetime("us", "UTC"))
            .alias(col)
        )
    return df.with_columns(
        pl.col(col).str.to_datetime(time_zone="UTC").cast(pl.Datetime("us", "UTC"))
    )


def compute_latency_from_live(live_df: pl.DataFrame) -> tuple[pl.DataFrame, LatencyStats]:
    """latency_ms = local_ts - exchange_ts on live captures."""
    df = live_df
    for col in (EXCHANGE_TS, LOCAL_TS):
        df = _ensure_datetime(df, col)

    # Datetime("us") → Int64 microseconds; divide by 1e3 → milliseconds.
    timed = df.drop_nulls([EXCHANGE_TS, LOCAL_TS]).with_columns(
        (
            (pl.col(LOCAL_TS).dt.epoch("us") - pl.col(EXCHANGE_TS).dt.epoch("us")) / 1_000.0
        ).alias("latency_ms")
    )
    if timed.is_empty():
        raise ValueError("Live data has no overlapping non-null exchange_ts/local_ts rows")

    # Drop pathological negatives heavily below -1s (clock issues)
    cleaned = timed.filter(pl.col("latency_ms") > -1000)
    if cleaned.is_empty():
        raise ValueError("All latency samples filtered out; check clock sync / units")

    q = cleaned.select(
        pl.col("latency_ms").count().alias("n"),
        pl.col("latency_ms").mean().alias("mean"),
        pl.col("latency_ms").std().alias("std"),
        pl.col("latency_ms").quantile(0.50).alias("p50"),
        pl.col("latency_ms").quantile(0.95).alias("p95"),
        pl.col("latency_ms").quantile(0.99).alias("p99"),
        pl.col("latency_ms").min().alias("min"),
        pl.col("latency_ms").max().alias("max"),
    ).row(0, named=True)

    stats = LatencyStats(
        n_samples=int(q["n"]),
        mean_ms=float(q["mean"] or 0.0),
        std_ms=float(q["std"] or 0.0),
        p50_ms=float(q["p50"] or 0.0),
        p95_ms=float(q["p95"] or 0.0),
        p99_ms=float(q["p99"] or 0.0),
        min_ms=float(q["min"] or 0.0),
        max_ms=float(q["max"] or 0.0),
        method="live_local_minus_exchange",
    )
    log.info("latency_stats_computed", **stats.to_dict())
    return cleaned, stats


def compare_live_vs_historical(
    live_df: pl.DataFrame,
    hist_df: pl.DataFrame,
) -> dict[str, Any]:
    """Overlap diagnostics: matched sequences / timestamps between live and hist."""
    live = _ensure_datetime(live_df, EXCHANGE_TS)
    hist = _ensure_datetime(hist_df, EXCHANGE_TS)

    summary: dict[str, Any] = {
        "live_rows": live.height,
        "hist_rows": hist.height,
    }

    if SYMBOL in live.columns and SYMBOL in hist.columns:
        live_syms = set(live.get_column(SYMBOL).unique().to_list())
        hist_syms = set(hist.get_column(SYMBOL).unique().to_list())
        summary["symbols_live"] = sorted(live_syms)
        summary["symbols_hist"] = sorted(hist_syms)
        summary["symbols_overlap"] = sorted(live_syms & hist_syms)

    if SEQUENCE in live.columns and SEQUENCE in hist.columns:
        joined = live.select(SEQUENCE, EXCHANGE_TS).join(
            hist.select(SEQUENCE, pl.col(EXCHANGE_TS).alias("hist_exchange_ts")),
            on=SEQUENCE,
            how="inner",
        )
        summary["matched_by_sequence"] = joined.height
    else:
        # Nearest timestamp match count within 1ms window (approx via join_asof)
        live_s = live.sort(EXCHANGE_TS).select(pl.col(EXCHANGE_TS).alias("live_ts"))
        hist_s = hist.sort(EXCHANGE_TS).select(pl.col(EXCHANGE_TS).alias("hist_ts"))
        asof = live_s.join_asof(
            hist_s,
            left_on="live_ts",
            right_on="hist_ts",
            strategy="nearest",
            tolerance="1ms",
        )
        matched = asof.filter(pl.col("hist_ts").is_not_null()).height
        summary["matched_by_exchange_ts_1ms"] = matched

    live_min = live.select(pl.col(EXCHANGE_TS).min()).item()
    live_max = live.select(pl.col(EXCHANGE_TS).max()).item()
    hist_min = hist.select(pl.col(EXCHANGE_TS).min()).item()
    hist_max = hist.select(pl.col(EXCHANGE_TS).max()).item()
    summary["live_exchange_ts_range"] = [str(live_min), str(live_max)]
    summary["hist_exchange_ts_range"] = [str(hist_min), str(hist_max)]
    log.info("live_vs_hist_compare", **summary)
    return summary


def apply_latency_correction(
    hist_df: pl.DataFrame,
    *,
    latency_ms: float,
    jitter_ms: float = 0.0,
    seed: int = 42,
    ts_col: str = EXCHANGE_TS,
    out_col: str = "effective_ts",
) -> pl.DataFrame:
    """Shift historical exchange timestamps by measured latency (+ optional jitter).

    effective_ts = exchange_ts + latency_ms (+ N(0, jitter) if jitter_ms > 0)
    """
    import numpy as np

    df = _ensure_datetime(hist_df, ts_col)
    n = df.height
    if jitter_ms > 0:
        rng = np.random.default_rng(seed)
        offset_ms = latency_ms + rng.normal(0.0, jitter_ms, size=n)
    else:
        offset_ms = np.full(n, latency_ms, dtype=np.float64)

    offset_us = (offset_ms * 1_000.0).astype(np.int64)
    df = df.with_columns(
        pl.from_epoch(pl.col(ts_col).dt.epoch("us") + pl.Series("_off_us", offset_us), time_unit="us")
        .dt.replace_time_zone("UTC")
        .cast(pl.Datetime("us", "UTC"))
        .alias(out_col),
        pl.lit(latency_ms).alias("applied_latency_ms"),
        pl.lit(jitter_ms).alias("applied_jitter_ms"),
    )
    log.info(
        "latency_applied",
        rows=df.height,
        latency_ms=latency_ms,
        jitter_ms=jitter_ms,
        out_col=out_col,
    )
    return df


def run_latency_pipeline(
    *,
    live_path: Path,
    hist_path: Path,
    out_dir: Path,
    latency_ms_override: float | None = None,
    apply_jitter: bool = False,
    compression: str = "zstd",
) -> dict[str, Any]:
    """Full compare → estimate → apply → persist under experiments/<run>/."""
    out_dir.mkdir(parents=True, exist_ok=True)
    live_df = read_parquet(live_path)
    hist_df = read_parquet(hist_path)

    compare = compare_live_vs_historical(live_df, hist_df)
    samples, stats = compute_latency_from_live(live_df)

    latency_ms = (
        float(latency_ms_override) if latency_ms_override is not None else stats.mean_ms
    )
    jitter_ms = stats.std_ms if apply_jitter else 0.0

    corrected = apply_latency_correction(
        hist_df,
        latency_ms=latency_ms,
        jitter_ms=jitter_ms,
    )
    corrected_path = out_dir / "historical_latency_corrected.parquet"
    write_parquet(corrected, corrected_path, compression=compression)

    samples_path = out_dir / "latency_samples.parquet"
    write_parquet(samples, samples_path, compression=compression)

    result = {
        "live_path": str(live_path),
        "hist_path": str(hist_path),
        "compare": compare,
        "stats": stats.to_dict(),
        "applied_latency_ms": latency_ms,
        "applied_jitter_ms": jitter_ms,
        "corrected_path": str(corrected_path),
        "samples_path": str(samples_path),
    }
    return result
