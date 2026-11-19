"""Shared helpers for free exchange public archives (Binance ZIP / Bybit gz)."""

from __future__ import annotations

import gzip
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import httpx
import polars as pl

from hft_loop.io import ensure_dir
from hft_loop.logging_setup import get_logger

log = get_logger("providers.archive_utils")


def parse_utc_date(value: str) -> date:
    text = value.strip().replace("Z", "+00:00")
    if len(text) == 10:
        return date.fromisoformat(text)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date()


def iter_archive_dates(start: str, end: str) -> list[date]:
    """UTC calendar days for daily archives.

    Inclusive start date, exclusive end date. If both resolve to the same
    calendar day (e.g. an intra-day window), that single day is returned.
    """
    start_d = parse_utc_date(start)
    end_d = parse_utc_date(end)
    if end_d < start_d:
        raise ValueError(f"end before start: start={start} end={end}")
    if end_d == start_d:
        return [start_d]
    out: list[date] = []
    cur = start_d
    while cur < end_d:
        out.append(cur)
        cur += timedelta(days=1)
    if not out:
        raise ValueError(f"empty date range: start={start} end={end}")
    return out


def download_bytes(
    url: str,
    *,
    timeout_sec: float = 180.0,
    client: httpx.Client | None = None,
) -> bytes:
    owns = client is None
    http = client or httpx.Client(timeout=timeout_sec, follow_redirects=True)
    try:
        resp = http.get(url)
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"Archive download timed out after {timeout_sec}s: {url}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Archive HTTP error for {url}: {exc}") from exc
    finally:
        if owns:
            http.close()

    if resp.status_code == 404:
        raise FileNotFoundError(f"Archive not found (HTTP 404): {url}")
    if resp.status_code >= 400:
        raise RuntimeError(f"Archive error HTTP {resp.status_code} for {url}: {resp.text[:300]}")
    return resp.content


def save_raw_bytes(content: bytes, path: Path) -> Path:
    ensure_dir(path.parent)
    path.write_bytes(content)
    log.info("archive_raw_saved", path=str(path), bytes=path.stat().st_size)
    return path


def extract_csv_text_from_zip(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            raise ValueError("ZIP archive contains no files")
        # Prefer a .csv member; otherwise first file.
        csv_names = [n for n in names if n.lower().endswith(".csv")]
        member = csv_names[0] if csv_names else names[0]
        raw = zf.read(member)
    return raw.decode("utf-8", errors="replace")


def extract_csv_text_from_gz(content: bytes) -> str:
    with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
        raw = gz.read()
    return raw.decode("utf-8", errors="replace")


def read_csv_flexible(text: str, *, columns: Iterable[str] | None = None) -> pl.DataFrame:
    """Parse CSV text; tolerate missing/present headers via polars inference."""
    if not text.strip():
        raise ValueError("CSV body is empty")
    buf = io.StringIO(text)
    # Peek first line to decide whether a header exists when columns are known.
    first = text.splitlines()[0]
    has_alpha_header = any(ch.isalpha() for ch in first)
    if columns is not None and not has_alpha_header:
        return pl.read_csv(
            buf,
            has_header=False,
            new_columns=list(columns),
            infer_schema_length=10_000,
            ignore_errors=True,
        )
    return pl.read_csv(buf, infer_schema_length=10_000, ignore_errors=True)


def concat_frames(frames: list[pl.DataFrame]) -> pl.DataFrame:
    if not frames:
        raise ValueError("No frames to concatenate")
    if len(frames) == 1:
        return frames[0]
    return pl.concat(frames, how="diagonal_relaxed")
