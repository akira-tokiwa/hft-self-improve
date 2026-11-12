"""Parquet read/write helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from hft_loop.logging_setup import get_logger

log = get_logger("hft_loop.io")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_parquet(
    df: pl.DataFrame,
    path: Path,
    *,
    compression: str = "zstd",
) -> Path:
    ensure_dir(path.parent)
    df.write_parquet(path, compression=compression)
    log.info(
        "parquet_written",
        path=str(path),
        rows=df.height,
        cols=df.width,
        compression=compression,
    )
    return path


def read_parquet(path: Path) -> pl.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Parquet not found: {path}")
    if path.is_dir():
        files = sorted(path.glob("**/*.parquet"))
        if not files:
            raise FileNotFoundError(f"No .parquet files under {path}")
        return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
    return pl.read_parquet(path)


def list_parquet_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file() and root.suffix == ".parquet":
        return [root]
    return sorted(root.rglob("*.parquet"), key=lambda p: p.stat().st_mtime)


def latest_parquet(root: Path) -> Path:
    files = list_parquet_files(root)
    if not files:
        raise FileNotFoundError(f"No parquet files found under {root}")
    return files[-1]


def parquet_path_for(
    root: Path,
    *,
    provider: str,
    symbol: str,
    dataset: str,
    stamp: str,
) -> Path:
    safe_symbol = symbol.replace("/", "-").replace(":", "-")
    return root / provider / safe_symbol / f"{dataset}_{stamp}.parquet"


def df_summary(df: pl.DataFrame) -> dict[str, Any]:
    return {
        "rows": df.height,
        "cols": df.width,
        "columns": df.columns,
        "dtypes": {c: str(t) for c, t in df.schema.items()},
    }
