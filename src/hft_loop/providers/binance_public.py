"""Binance public daily archives from data.binance.vision (no API key).

Supports:
  venue: spot | um | cm
  dataset: trades | aggTrades

URLs (UTC day files):
  spot trades:
    https://data.binance.vision/data/spot/daily/trades/{SYM}/{SYM}-trades-{YYYY-MM-DD}.zip
  spot aggTrades:
    https://data.binance.vision/data/spot/daily/aggTrades/{SYM}/{SYM}-aggTrades-{YYYY-MM-DD}.zip
  um/cm futures trades / aggTrades under data/futures/{um|cm}/daily/...
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
    extract_csv_text_from_zip,
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

log = get_logger("providers.binance_public")

BASE_URL = "https://data.binance.vision/data"

_VENUE_ALIASES = {
    "spot": "spot",
    "um": "um",
    "cm": "cm",
    "usdm": "um",
    "coinm": "cm",
    "futures": "um",
    "futures-um": "um",
    "futures-cm": "cm",
    "um-futures": "um",
    "cm-futures": "cm",
}

_TRADE_COLS = ("id", "price", "qty", "quote_qty", "time", "is_buyer_maker", "is_best_match")
_AGG_COLS = (
    "agg_trade_id",
    "price",
    "qty",
    "first_trade_id",
    "last_trade_id",
    "time",
    "is_buyer_maker",
    "is_best_match",
)


class BinancePublicProvider:
    name = "binance_public"

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
            url = self._url(market=market, dataset=dataset, symbol=symbol, day=day.isoformat())
            urls.append(url)
            try:
                content = download_bytes(url, timeout_sec=self.timeout_sec)
            except FileNotFoundError:
                missing.append(url)
                log.warning("binance_day_missing", url=url, day=day.isoformat())
                continue

            raw_name = f"{symbol}-{dataset}-{day.isoformat()}.zip"
            raw_path = raw_dir / self.name / market / symbol / raw_name
            save_raw_bytes(content, raw_path)
            raw_paths.append(raw_path)

            text = extract_csv_text_from_zip(content)
            expected = _AGG_COLS if dataset == "aggTrades" else _TRADE_COLS
            # Spot trades sometimes include is_best_match; futures often omit it.
            df_raw = read_csv_flexible(text, columns=expected[:6] if market != "spot" else expected)
            frames.append(
                self._normalize_frame(
                    df_raw,
                    symbol=symbol,
                    venue=market,
                    dataset=dataset,
                )
            )

        if not frames:
            raise FileNotFoundError(
                "No Binance archive days downloaded. "
                f"Tried {len(urls)} URL(s); missing={len(missing)}. "
                "Confirm symbol/venue/dataset and that the UTC day file is published "
                "(usually next day)."
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
        key = venue.strip().lower().replace("_", "-")
        if key not in _VENUE_ALIASES:
            known = ", ".join(sorted(set(_VENUE_ALIASES.values())))
            raise ValueError(
                f"Unknown Binance venue '{venue}'. Use one of: {known} "
                "(aliases: spot, um, cm, futures-um, futures-cm)."
            )
        return _VENUE_ALIASES[key]

    @staticmethod
    def _normalize_dataset(dataset: str) -> str:
        key = dataset.strip().lower().replace("-", "").replace("_", "")
        if key in {"trades", "trade"}:
            return "trades"
        if key in {"aggtrades", "aggtrade"}:
            return "aggTrades"
        raise ValueError(
            f"Unsupported Binance dataset '{dataset}'. Use trades or aggTrades."
        )

    @staticmethod
    def _url(*, market: str, dataset: str, symbol: str, day: str) -> str:
        file_stub = "aggTrades" if dataset == "aggTrades" else "trades"
        fname = f"{symbol}-{file_stub}-{day}.zip"
        if market == "spot":
            return f"{BASE_URL}/spot/daily/{file_stub}/{symbol}/{fname}"
        return f"{BASE_URL}/futures/{market}/daily/{file_stub}/{symbol}/{fname}"

    def _normalize_frame(
        self,
        df: pl.DataFrame,
        *,
        symbol: str,
        venue: str,
        dataset: str,
    ) -> pl.DataFrame:
        # Normalize header variants: quantity/qty, transact_time/time, etc.
        cols = {c.lower().replace("-", "").replace("_", ""): c for c in df.columns}
        price_c = cols.get("price")
        qty_c = cols.get("qty") or cols.get("quantity")
        time_c = (
            cols.get("time")
            or cols.get("timestamp")
            or cols.get("transacttime")
            or cols.get("transtime")
        )
        maker_c = cols.get("isbuyermaker")
        id_c = cols.get("id") or cols.get("aggtradeid") or cols.get("a")
        if not price_c or not qty_c or not time_c:
            raise ValueError(
                f"Unexpected Binance CSV columns for {dataset}: {df.columns}"
            )

        # Side: is_buyer_maker True => taker sold => side sell
        if maker_c:
            maker_txt = pl.col(maker_c).cast(pl.Utf8).str.to_lowercase()
            side_expr = (
                pl.when(maker_txt.is_in(["true", "1"]))
                .then(pl.lit("sell"))
                .otherwise(pl.lit("buy"))
                .alias(SIDE)
            )
        else:
            side_expr = pl.lit(None).cast(pl.Utf8).alias(SIDE)

        seq_expr = (
            pl.col(id_c).cast(pl.Int64, strict=False).alias(SEQUENCE)
            if id_c
            else pl.arange(1, df.height + 1).alias(SEQUENCE)
        )

        # Binance times are usually ms; Vision sometimes publishes µs.
        ts_num = pl.col(time_c).cast(pl.Int64, strict=False)
        exchange_expr = (
            pl.when(ts_num > 10_000_000_000_000)  # > ~2286 in ms ⇒ treat as µs
            .then(ts_num)
            .otherwise(ts_num * 1000)
            .cast(pl.Datetime("us", "UTC"))
            .alias(EXCHANGE_TS)
        )

        out = df.select(
            exchange_expr,
            pl.lit(None).cast(pl.Datetime("us", "UTC")).alias(LOCAL_TS),
            pl.lit(symbol).alias(SYMBOL),
            pl.col(price_c).cast(pl.Float64).alias(PRICE),
            pl.col(qty_c).cast(pl.Float64).alias(SIZE),
            side_expr,
            seq_expr,
            pl.lit(self.name).alias(PROVIDER),
            pl.lit(venue).alias(VENUE),
        ).drop_nulls([EXCHANGE_TS, PRICE, SIZE])
        if out.height == 0:
            raise ValueError(f"Parsed 0 trades from Binance {dataset} for {symbol}")
        return out
