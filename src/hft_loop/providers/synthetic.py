"""Generate synthetic trade ticks for offline / dry-run pipelines."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl

from hft_loop.io import parquet_path_for, write_parquet
from hft_loop.logging_setup import get_logger
from hft_loop.providers.base import DownloadRequest, DownloadResult
from hft_loop.schema import (
    EXCHANGE_TS,
    LOCAL_TS,
    PRICE,
    PROVIDER,
    SEQUENCE,
    SIDE,
    SIZE,
    SYMBOL,
    VENUE,
)

log = get_logger("providers.synthetic")


def _parse_date(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    if len(text) == 10:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def generate_trades(
    *,
    symbol: str,
    venue: str,
    provider: str,
    start: datetime,
    end: datetime,
    n_rows: int = 500,
    seed: int = 42,
    include_local_ts: bool = False,
    latency_ms_mean: float = 4.0,
    latency_ms_jitter: float = 1.5,
) -> pl.DataFrame:
    if end <= start:
        raise ValueError(f"end must be after start: start={start} end={end}")
    if n_rows < 1:
        raise ValueError("n_rows must be >= 1")

    rng = np.random.default_rng(seed)
    span_ns = int((end - start).total_seconds() * 1e9)
    offsets = np.sort(rng.integers(0, max(span_ns, 1), size=n_rows))
    exchange = [start + timedelta(microseconds=int(o // 1000)) for o in offsets]

    mid = 50_000.0
    rets = rng.normal(0.0, 0.0002, size=n_rows)
    prices = mid * np.cumprod(1.0 + rets)
    sizes = rng.uniform(0.001, 0.5, size=n_rows)
    sides = rng.choice(["buy", "sell"], size=n_rows)

    local: list[datetime | None]
    if include_local_ts:
        lag_ms = rng.normal(latency_ms_mean, latency_ms_jitter, size=n_rows)
        lag_ms = np.clip(lag_ms, 0.1, None)
        local = [
            ts + timedelta(milliseconds=float(lag))
            for ts, lag in zip(exchange, lag_ms, strict=True)
        ]
    else:
        local = [None] * n_rows

    return pl.DataFrame(
        {
            EXCHANGE_TS: exchange,
            LOCAL_TS: local,
            SYMBOL: [symbol] * n_rows,
            PRICE: prices,
            SIZE: sizes,
            SIDE: sides,
            SEQUENCE: list(range(1, n_rows + 1)),
            PROVIDER: [provider] * n_rows,
            VENUE: [venue] * n_rows,
        }
    ).with_columns(
        pl.col(EXCHANGE_TS).cast(pl.Datetime("us", "UTC")),
        pl.col(LOCAL_TS).cast(pl.Datetime("us", "UTC")),
    )


class SyntheticProvider:
    name = "synthetic"

    def download(
        self,
        request: DownloadRequest,
        *,
        raw_dir: Path,
        parquet_dir: Path,
        compression: str,
    ) -> DownloadResult:
        start = _parse_date(request.start)
        end = _parse_date(request.end)
        n_rows = int(request.extra.get("n_rows", 500))
        seed = int(request.extra.get("seed", 42))
        as_live = bool(request.extra.get("as_live", False))

        df = generate_trades(
            symbol=request.symbol,
            venue=request.venue,
            provider=request.provider if request.provider != "synthetic" else "synthetic",
            start=start,
            end=end,
            n_rows=n_rows,
            seed=seed,
            include_local_ts=as_live,
            latency_ms_mean=float(request.extra.get("latency_ms_mean", 4.0)),
            latency_ms_jitter=float(request.extra.get("latency_ms_jitter", 1.5)),
        )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_root = parquet_dir if request.output_format == "parquet" else raw_dir
        # Live samples land under data/live when requested.
        if as_live and "live_dir" in request.extra:
            out_root = Path(request.extra["live_dir"])

        path = parquet_path_for(
            out_root,
            provider=request.provider,
            symbol=request.symbol,
            dataset=request.dataset,
            stamp=stamp,
        )
        if request.output_format == "parquet" or as_live:
            write_parquet(df, path, compression=compression)
        else:
            path = path.with_suffix(".csv")
            path.parent.mkdir(parents=True, exist_ok=True)
            df.write_csv(path)
            log.info("raw_csv_written", path=str(path), rows=df.height)

        log.info(
            "synthetic_download_done",
            rows=df.height,
            path=str(path),
            as_live=as_live,
        )
        return DownloadResult(
            provider=self.name,
            symbol=request.symbol,
            paths=[path],
            rows=df.height,
            dry_run=True,
            meta={"start": request.start, "end": request.end, "dataset": request.dataset},
        )
