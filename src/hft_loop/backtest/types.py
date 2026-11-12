"""Structured backtest result objects (Bot-friendly JSON)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TradeRecord:
    trade_id: int
    entry_ts: str
    exit_ts: str
    side: str  # long | short
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    return_pct: float
    fees: float


@dataclass
class BacktestMetrics:
    total_pnl: float
    total_return_pct: float
    sharpe: float | None
    sortino: float | None
    max_drawdown_pct: float
    max_drawdown_abs: float
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float | None
    avg_trade_pnl: float | None
    profit_factor: float | None
    final_equity: float
    initial_capital: float
    exposure_frac: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestResult:
    ok: bool
    run_id: str
    strategy: str
    strategy_params: dict[str, Any]
    data_path: str
    symbol: str | None
    device: str
    latency_ms: float
    latency_jitter_ms: float
    fee_bps: float
    slippage_bps: float
    metrics: BacktestMetrics
    n_bars: int
    run_dir: str
    artifacts: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def to_summary_dict(self) -> dict[str, Any]:
        """Compact JSON for stdout / Bot."""
        m = self.metrics.to_dict()
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "data_path": self.data_path,
            "device": self.device,
            "latency_ms": self.latency_ms,
            "latency_jitter_ms": self.latency_jitter_ms,
            "n_bars": self.n_bars,
            "metrics": {
                "total_pnl": m["total_pnl"],
                "total_return_pct": m["total_return_pct"],
                "sharpe": m["sharpe"],
                "sortino": m["sortino"],
                "max_drawdown_pct": m["max_drawdown_pct"],
                "n_trades": m["n_trades"],
                "win_rate": m["win_rate"],
                "profit_factor": m["profit_factor"],
                "final_equity": m["final_equity"],
            },
            "run_dir": self.run_dir,
            "artifacts": self.artifacts,
            "error": self.error,
        }

    def to_full_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["metrics"] = self.metrics.to_dict()
        return d
