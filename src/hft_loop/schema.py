"""Canonical column names for market-data Parquet files."""

from __future__ import annotations

EXCHANGE_TS = "exchange_ts"
LOCAL_TS = "local_ts"
SYMBOL = "symbol"
PRICE = "price"
SIZE = "size"
SIDE = "side"
SEQUENCE = "sequence"
PROVIDER = "provider"
VENUE = "venue"

TRADE_COLUMNS = (
    EXCHANGE_TS,
    LOCAL_TS,
    SYMBOL,
    PRICE,
    SIZE,
    SIDE,
    SEQUENCE,
    PROVIDER,
    VENUE,
)

# Historical dumps often lack local receive time.
HISTORICAL_REQUIRED = (EXCHANGE_TS, SYMBOL, PRICE, SIZE)
LIVE_REQUIRED = (EXCHANGE_TS, LOCAL_TS, SYMBOL, PRICE, SIZE)
