"""Tardis.dev historical download scaffold.

Uses TARDIS_API_KEY from the environment. Without a key, callers should pass dry_run.
HTTP shape follows Tardis datasets API; swap in tardis-client later if needed.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from hft_loop.env import require_env
from hft_loop.io import ensure_dir, parquet_path_for, write_parquet
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

log = get_logger("providers.tardis")

DEFAULT_BASE_URL = "https://api.tardis.dev/v1"


class TardisProvider:
    name = "tardis"

    def __init__(self, base_url: str | None = None, timeout_sec: float = 120.0) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_sec = timeout_sec

    def download(
        self,
        request: DownloadRequest,
        *,
        raw_dir: Path,
        parquet_dir: Path,
        compression: str,
    ) -> DownloadResult:
        api_key = require_env("TARDIS_API_KEY")
        exchange = request.venue
        data_type = request.dataset
        # Tardis path style: /data-feeds/{exchange}/{dataType}/{from}/{to}/{symbol}.csv.gz
        # Exact routes vary by plan; this scaffold hits the documented data-feeds endpoint.
        symbol = request.symbol
        url = (
            f"{self.base_url}/data-feeds/{exchange}/{data_type}/"
            f"{request.start}/{request.end}/{symbol}"
        )
        headers = {"Authorization": f"Bearer {api_key}"}
        params: dict[str, Any] = {}
        if request.extra.get("offset"):
            params["offset"] = request.extra["offset"]

        log.info(
            "tardis_download_start",
            url=url,
            symbol=symbol,
            exchange=exchange,
            data_type=data_type,
        )

        try:
            with httpx.Client(timeout=self.timeout_sec, follow_redirects=True) as client:
                resp = client.get(url, headers=headers, params=params or None)
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"Tardis request timed out after {self.timeout_sec}s: {url}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Tardis HTTP error: {exc}") from exc

        if resp.status_code == 401:
            raise PermissionError("Tardis authentication failed (check TARDIS_API_KEY).")
        if resp.status_code == 404:
            raise FileNotFoundError(
                f"Tardis data not found for {exchange}/{symbol}/{data_type} "
                f"[{request.start} .. {request.end}]. Confirm venue/symbol/dataset."
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Tardis error HTTP {resp.status_code}: {resp.text[:500]}"
            )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        raw_path = parquet_path_for(
            raw_dir,
            provider=self.name,
            symbol=symbol,
            dataset=data_type,
            stamp=stamp,
        ).with_suffix(".csv")
        ensure_dir(raw_path.parent)
        raw_path.write_bytes(resp.content)
        log.info("tardis_raw_saved", path=str(raw_path), bytes=raw_path.stat().st_size)

        paths: list[Path] = [raw_path]
        rows = 0
        if request.output_format == "parquet":
            df = self._csv_bytes_to_frame(
                resp.content,
                symbol=symbol,
                venue=exchange,
            )
            pq = parquet_path_for(
                parquet_dir,
                provider=self.name,
                symbol=symbol,
                dataset=data_type,
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
                "url": url,
                "status_code": resp.status_code,
                "start": request.start,
                "end": request.end,
                "dataset": data_type,
            },
        )

    def _csv_bytes_to_frame(
        self,
        content: bytes,
        *,
        symbol: str,
        venue: str,
    ) -> pl.DataFrame:
        """Best-effort normalize common Tardis CSV trade columns."""
        text = content.decode("utf-8", errors="replace")
        # Skip empty
        if not text.strip():
            raise ValueError("Tardis response body is empty")

        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise ValueError("Tardis CSV has no header row")

        rows_out: list[dict[str, Any]] = []
        for i, row in enumerate(reader, start=1):
            ts_raw = (
                row.get("timestamp")
                or row.get("exchange_timestamp")
                or row.get("local_timestamp")
            )
            if ts_raw is None:
                continue
            # Tardis often uses microseconds since epoch as int string
            try:
                ts_int = int(float(ts_raw))
                # Heuristic: ns vs us vs ms
                if ts_int > 1e17:
                    exchange_ts = datetime.fromtimestamp(ts_int / 1e9, tz=timezone.utc)
                elif ts_int > 1e14:
                    exchange_ts = datetime.fromtimestamp(ts_int / 1e6, tz=timezone.utc)
                elif ts_int > 1e11:
                    exchange_ts = datetime.fromtimestamp(ts_int / 1e3, tz=timezone.utc)
                else:
                    exchange_ts = datetime.fromtimestamp(ts_int, tz=timezone.utc)
            except (TypeError, ValueError):
                try:
                    exchange_ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except ValueError:
                    continue

            price = row.get("price") or row.get("amount")
            size = row.get("amount") if row.get("price") else row.get("size")
            if price is None:
                price = row.get("bid_price") or row.get("ask_price")
            if size is None:
                size = row.get("bid_amount") or row.get("ask_amount") or 0

            side = (row.get("side") or "").lower() or None
            seq = row.get("id") or row.get("trade_id") or i

            try:
                price_f = float(price) if price is not None else None
                size_f = float(size) if size is not None else 0.0
            except (TypeError, ValueError):
                continue
            if price_f is None:
                continue

            rows_out.append(
                {
                    EXCHANGE_TS: exchange_ts,
                    LOCAL_TS: None,
                    SYMBOL: row.get("symbol") or symbol,
                    PRICE: price_f,
                    SIZE: size_f,
                    SIDE: side,
                    SEQUENCE: int(seq) if str(seq).isdigit() else i,
                    PROVIDER: self.name,
                    VENUE: venue,
                }
            )

        if not rows_out:
            raise ValueError(
                "Failed to parse any rows from Tardis CSV. "
                "Check dataset type or extend _csv_bytes_to_frame mapping."
            )

        return pl.DataFrame(rows_out).with_columns(
            pl.col(EXCHANGE_TS).cast(pl.Datetime("us", "UTC")),
            pl.col(LOCAL_TS).cast(pl.Datetime("us", "UTC")),
        )
