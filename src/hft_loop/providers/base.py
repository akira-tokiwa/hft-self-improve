"""Provider interface and factory."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class DownloadRequest:
    provider: str
    symbol: str
    venue: str
    dataset: str  # e.g. trades | book_snapshot_5 | mbp-1
    start: str  # ISO date or datetime (UTC)
    end: str
    dry_run: bool = False
    output_format: str = "parquet"  # parquet | raw
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DownloadResult:
    provider: str
    symbol: str
    paths: list[Path]
    rows: int
    dry_run: bool
    meta: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def download(
        self,
        request: DownloadRequest,
        *,
        raw_dir: Path,
        parquet_dir: Path,
        compression: str,
    ) -> DownloadResult: ...


def get_provider(name: str, *, dry_run: bool = False) -> Provider:
    key = name.strip().lower()
    if dry_run or key in {"synthetic", "demo", "dry_run"}:
        from hft_loop.providers.synthetic import SyntheticProvider

        return SyntheticProvider()
    if key == "tardis":
        from hft_loop.providers.tardis import TardisProvider

        return TardisProvider()
    if key == "databento":
        from hft_loop.providers.databento_provider import DatabentoProvider

        return DatabentoProvider()
    raise ValueError(
        f"Unknown provider '{name}'. Supported: tardis, databento, synthetic (dry-run)."
    )
