"""Databento historical download scaffold.

Prefers the official `databento` package when installed. Falls back to a clear
error directing the operator to install it or use --dry-run.
API key: DATABENTO_API_KEY.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from hft_loop.env import require_env
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

log = get_logger("providers.databento")


class DatabentoProvider:
    name = "databento"

    def download(
        self,
        request: DownloadRequest,
        *,
        raw_dir: Path,
        parquet_dir: Path,
        compression: str,
    ) -> DownloadResult:
        api_key = require_env("DATABENTO_API_KEY")
        try:
            import databento as db  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "databento package is not installed. "
                "Run: pip install databento   — or use --dry-run for synthetic data."
            ) from exc

        dataset = request.extra.get("db_dataset") or request.venue
        schema = request.dataset
        symbols = [request.symbol]
        stype_in = request.extra.get("stype_in", "raw_symbol")

        log.info(
            "databento_download_start",
            dataset=dataset,
            schema=schema,
            symbols=symbols,
            start=request.start,
            end=request.end,
        )

        try:
            client = db.Historical(key=api_key)
            store = client.timeseries.get_range(
                dataset=dataset,
                symbols=symbols,
                schema=schema,
                start=request.start,
                end=request.end,
                stype_in=stype_in,
            )
        except Exception as exc:  # noqa: BLE001 — surface provider errors cleanly
            raise RuntimeError(f"Databento download failed: {exc}") from exc

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        raw_path = parquet_path_for(
            raw_dir,
            provider=self.name,
            symbol=request.symbol,
            dataset=schema,
            stamp=stamp,
        ).with_suffix(".dbn")
        raw_path.parent.mkdir(parents=True, exist_ok=True)

        # DBNStore supports to_file / to_df depending on version
        try:
            store.to_file(path=str(raw_path))
        except Exception:
            # Some versions use different APIs; keep a dataframe fallback path.
            log.warning("databento_to_file_failed_fallback_df")

        df_pd = store.to_df()
        df = self._normalize_df(df_pd, symbol=request.symbol, venue=str(dataset))
        paths: list[Path] = []
        if raw_path.exists():
            paths.append(raw_path)

        rows = 0
        if request.output_format == "parquet":
            pq = parquet_path_for(
                parquet_dir,
                provider=self.name,
                symbol=request.symbol,
                dataset=schema,
                stamp=stamp,
            )
            write_parquet(df, pq, compression=compression)
            paths.append(pq)
            rows = df.height
        else:
            csv_path = raw_path.with_suffix(".csv")
            df.write_csv(csv_path)
            paths.append(csv_path)
            rows = df.height

        return DownloadResult(
            provider=self.name,
            symbol=request.symbol,
            paths=paths,
            rows=rows,
            dry_run=False,
            meta={
                "dataset": dataset,
                "schema": schema,
                "start": request.start,
                "end": request.end,
            },
        )

    def _normalize_df(self, df_pd: Any, *, symbol: str, venue: str) -> pl.DataFrame:
        df = pl.from_pandas(df_pd.reset_index())
        cols = {c.lower(): c for c in df.columns}

        def pick(*names: str) -> str | None:
            for n in names:
                if n in cols:
                    return cols[n]
                if n in df.columns:
                    return n
            return None

        ts_col = pick("ts_event", "ts_recv", "timestamp", "exchange_ts")
        price_col = pick("price", "px")
        size_col = pick("size", "quantity", "amount")
        side_col = pick("side")
        seq_col = pick("sequence", "flags")

        if ts_col is None or price_col is None:
            raise ValueError(
                f"Cannot normalize Databento frame; columns={df.columns}. "
                "Extend _normalize_df for this schema."
            )

        exchange = pl.col(ts_col)
        # Databento timestamps are often datetime already; cast defensively.
        out = df.select(
            exchange.alias(EXCHANGE_TS),
            pl.lit(None).cast(pl.Datetime("us", "UTC")).alias(LOCAL_TS),
            pl.lit(symbol).alias(SYMBOL),
            pl.col(price_col).cast(pl.Float64).alias(PRICE),
            (
                pl.col(size_col).cast(pl.Float64).alias(SIZE)
                if size_col
                else pl.lit(0.0).alias(SIZE)
            ),
            (
                pl.col(side_col).cast(pl.Utf8).alias(SIDE)
                if side_col
                else pl.lit(None).cast(pl.Utf8).alias(SIDE)
            ),
            (
                pl.col(seq_col).cast(pl.Int64).alias(SEQUENCE)
                if seq_col
                else pl.arange(1, df.height + 1).alias(SEQUENCE)
            ),
            pl.lit(self.name).alias(PROVIDER),
            pl.lit(venue).alias(VENUE),
        ).with_columns(
            pl.col(EXCHANGE_TS).cast(pl.Datetime("us", "UTC"), strict=False),
        )
        log.info("databento_normalized", rows=out.height, columns=out.columns)
        return out
