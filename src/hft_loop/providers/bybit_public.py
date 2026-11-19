"""Bybit public daily trade archives from public.bybit.com (no API key).

Supports:
  venue: trading | spot   (trading covers linear + inverse by symbol)
  dataset: trades

URLs (UTC day files):
  trading (perps):
    https://public.bybit.com/trading/{SYM}/{SYM}{YYYY-MM-DD}.csv.gz
  spot:
    https://public.bybit.com/spot/{SYM}/{SYM}_{YYYY-MM-DD}.csv.gz
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from hft_loop.io import parquet_path_for, write_parquet
from hft_loop.logging_setup import get_logger
from hft_loop.providers.archive_utils import (
    concat_frames,
    download_bytes,
    extract_csv_text_from_gz,
    iter_archive_dates,
    read_csv_flexible,
    save_raw_bytes,
)
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

log = get_logger("providers.bybit_public")

BASE_URL = "https://public.bybit.com"

_VENUE_ALIASES = {
    "trading": "trading",
    "linear": "trading",
    "inverse": "trading",
    "perp": "trading",
    "perpetual": "trading",
    "spot": "spot",
}


class BybitPublicProvider:
    name = "bybit_public"

    def __init__(self, timeout_sec: float = 180.0) -> None:
        self.timeout_sec = timeout_sec

    def download(
        self,
        request: DownloadRequest,
        *,
        raw_dir: Path,
        parquet_dir: Path,
        compression: str,
    ) -> DownloadResult:
        market = self._normalize_venue(request.venue)
        dataset = self._normalize_dataset(request.dataset)
        symbol = request.symbol.strip().upper()
        days = iter_archive_dates(request.start, request.end)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        raw_paths: list[Path] = []
        frames: list[pl.DataFrame] = []
        urls: list[str] = []
        missing: list[str] = []

        for day in days:
            url = self._url(market=market, symbol=symbol, day=day.isoformat())
            urls.append(url)
            try:
                content = download_bytes(url, timeout_sec=self.timeout_sec)
            except FileNotFoundError:
                missing.append(url)
                log.warning("bybit_day_missing", url=url, day=day.isoformat())
                continue

            raw_name = (
                f"{symbol}{day.isoformat()}.csv.gz"
                if market == "trading"
                else f"{symbol}_{day.isoformat()}.csv.gz"
            )
            raw_path = raw_dir / self.name / market / symbol / raw_name
            save_raw_bytes(content, raw_path)
            raw_paths.append(raw_path)

            text = extract_csv_text_from_gz(content)
            df_raw = read_csv_flexible(text)
            frames.append(
                self._normalize_frame(df_raw, symbol=symbol, venue=market)
            )

        if not frames:
            raise FileNotFoundError(
                "No Bybit archive days downloaded. "
                f"Tried {len(urls)} URL(s); missing={len(missing)}. "
                "Confirm symbol/venue and that the UTC day file is published."
            )

        df = concat_frames(frames).sort(EXCHANGE_TS)
        paths: list[Path] = list(raw_paths)
        rows = 0
        if request.output_format == "parquet":
            pq = parquet_path_for(
                parquet_dir,
                provider=self.name,
                symbol=f"{market}-{symbol}",
                dataset=dataset,
                stamp=stamp,
            )
            write_parquet(df, pq, compression=compression)
            paths.append(pq)
            rows = df.height

        return DownloadResult(
            provider=self.name,
            symbol=symbol,
            paths=paths,
            rows=rows,
            dry_run=False,
            meta={
                "market": market,
                "dataset": dataset,
                "days": [d.isoformat() for d in days],
                "urls": urls,
                "missing_urls": missing,
                "start": request.start,
                "end": request.end,
            },
        )

    @staticmethod
    def _normalize_venue(venue: str) -> str:
        key = venue.strip().lower()
        if key not in _VENUE_ALIASES:
            known = ", ".join(sorted(set(_VENUE_ALIASES.values())))
            raise ValueError(
                f"Unknown Bybit venue '{venue}'. Use one of: {known} "
                "(aliases: trading, linear, inverse, spot)."
            )
        return _VENUE_ALIASES[key]

    @staticmethod
    def _normalize_dataset(dataset: str) -> str:
        key = dataset.strip().lower()
        if key in {"trades", "trade", "trading"}:
            return "trades"
        raise ValueError(
            f"Unsupported Bybit dataset '{dataset}'. Only 'trades' is supported "
            "(order-book ZIP lives under quote-saver.bycsi.com; not wired yet)."
        )

    @staticmethod
    def _url(*, market: str, symbol: str, day: str) -> str:
        if market == "spot":
            return f"{BASE_URL}/spot/{symbol}/{symbol}_{day}.csv.gz"
        return f"{BASE_URL}/trading/{symbol}/{symbol}{day}.csv.gz"

    def _normalize_frame(
        self,
        df: pl.DataFrame,
        *,
        symbol: str,
        venue: str,
    ) -> pl.DataFrame:
        cols = {c.lower(): c for c in df.columns}
        price_c = cols.get("price")
        size_c = cols.get("size") or cols.get("volume") or cols.get("qty")
        time_c = cols.get("timestamp") or cols.get("time")
        side_c = cols.get("side")
        id_c = cols.get("id") or cols.get("trdmatchid") or cols.get("execid")
        if not price_c or not size_c or not time_c:
            raise ValueError(f"Unexpected Bybit CSV columns: {df.columns}")

        ts_num = pl.col(time_c).cast(pl.Float64, strict=False)
        # Spot uses ms integers; trading (perp) uses unix seconds with fraction.
        exchange_expr = (
            pl.when(ts_num > 10_000_000_000)  # clearly ms
            .then((ts_num * 1000).cast(pl.Int64))
            .otherwise((ts_num * 1_000_000).cast(pl.Int64))
            .cast(pl.Datetime("us", "UTC"))
            .alias(EXCHANGE_TS)
        )

        if side_c:
            side_expr = (
                pl.col(side_c)
                .cast(pl.Utf8)
                .str.to_lowercase()
                .alias(SIDE)
            )
        else:
            side_expr = pl.lit(None).cast(pl.Utf8).alias(SIDE)

        if id_c and df[id_c].dtype in (pl.Int64, pl.Int32, pl.UInt64, pl.UInt32):
            seq_expr = pl.col(id_c).cast(pl.Int64).alias(SEQUENCE)
        else:
            # UUID trade ids (perps) — use row order within the day.
            seq_expr = pl.arange(1, df.height + 1).alias(SEQUENCE)

        out = (
            df.select(
                exchange_expr,
                pl.lit(None).cast(pl.Datetime("us", "UTC")).alias(LOCAL_TS),
                pl.lit(symbol).alias(SYMBOL),
                pl.col(price_c).cast(pl.Float64).alias(PRICE),
                pl.col(size_c).cast(pl.Float64).alias(SIZE),
                side_expr,
                seq_expr,
                pl.lit(self.name).alias(PROVIDER),
                pl.lit(venue).alias(VENUE),
            )
            .drop_nulls([EXCHANGE_TS, PRICE, SIZE])
            .with_columns(
                pl.when(pl.col(SIDE).is_in(["buy", "b"]))
                .then(pl.lit("buy"))
                .when(pl.col(SIDE).is_in(["sell", "s"]))
                .then(pl.lit("sell"))
                .otherwise(pl.col(SIDE))
                .alias(SIDE)
            )
        )
        if out.height == 0:
            raise ValueError(f"Parsed 0 trades from Bybit archive for {symbol}")
        return out
