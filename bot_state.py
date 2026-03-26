"""
Shared singleton state for the web dashboard.

MarketMaker writes to this state on every tick; the dashboard reads it.
Thread-safe: asyncio is single-threaded, and the FastAPI JSON endpoint
serialises via dict copy.
"""
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class TradeSnapshot:
    """Minimal trade record for dashboard display."""
    timestamp: float
    window_start: int
    side: str
    entry_price: float
    size_usdc: float
    p_signal: float
    won: bool
    pnl: float
    confidence: float = 0.0
    exit_type: str = "binary"


@dataclass
class BotState:
    """Live bot state — updated by MarketMaker, read by dashboard."""
    # Connection
    started_at: float = field(default_factory=time.time)
    wallet: str = ""
    paper_trading: bool = False
    paper_balance: float = 0.0

    # Market
    btc_price: float = 0.0
    btc_open_price: float = 0.0
    p_up: float = 0.5
    fair_yes: float = 0.5
    fair_no: float = 0.5
    candle_vol_bps: float = 0.0

    # Signal quality
    signal_confidence: float = 0.0
    signal_raw_p_up: float = 0.5
    signal_factors: Dict[str, float] = field(default_factory=dict)

    # Current window
    window_start: int = 0
    window_end: int = 0
    seconds_to_close: float = 0.0
    phase: str = "initializing"
    spread: float = 0.0
    realized_sigma: float = 0.0
    hourly_trend_bias: float = 0.0
    obi: float = 0.0
    vol_regime: str = "normal"
    volume_ratio: float = 1.0
    hedge_timeout_active: bool = False
    tick_momentum: float = 0.0

    # Orders
    yes_order_active: bool = False
    no_order_active: bool = False
    yes_order_price: float = 0.0
    no_order_price: float = 0.0

    # Expected value of current quotes
    yes_ev: float = 0.0
    no_ev: float = 0.0

    # Polymarket market prices
    market_yes_ask: Optional[float] = None
    market_no_ask: Optional[float] = None

    # Stats
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    stop_losses: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    rolling_win_rate: float = 0.0

    # Advanced stats
    sharpe_ratio: Optional[float] = None
    max_drawdown: float = 0.0
    profit_factor: Optional[float] = None
    consecutive_losses: int = 0
    max_win_streak: int = 0
    max_loss_streak: int = 0

    # Recent trades (last 50)
    recent_trades: List[TradeSnapshot] = field(default_factory=list)

    # Timestamp of last update
    last_update: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        """Serialise for JSON API."""
        uptime = time.time() - self.started_at
        return {
            "uptime_seconds": round(uptime, 1),
            "wallet": self.wallet,
            "paper_trading": self.paper_trading,
            "paper_balance": round(self.paper_balance, 2),
            "btc_price": round(self.btc_price, 2),
            "btc_open_price": round(self.btc_open_price, 2),
            "p_up": round(self.p_up, 4),
            "fair_yes": round(self.fair_yes, 4),
            "fair_no": round(self.fair_no, 4),
            "candle_vol_bps": round(self.candle_vol_bps, 1),
            "signal_confidence": round(self.signal_confidence, 3),
            "signal_raw_p_up": round(self.signal_raw_p_up, 4),
            "signal_factors": {k: round(v, 4) for k, v in self.signal_factors.items()},
            "window_start": self.window_start,
            "window_end": self.window_end,
            "seconds_to_close": round(self.seconds_to_close, 1),
            "phase": self.phase,
            "spread": round(self.spread, 4),
            "realized_sigma": round(self.realized_sigma, 6),
            "hourly_trend_bias": round(self.hourly_trend_bias, 4),
            "obi": round(self.obi, 4),
            "vol_regime": self.vol_regime,
            "volume_ratio": round(self.volume_ratio, 2),
            "hedge_timeout_active": self.hedge_timeout_active,
            "tick_momentum": round(self.tick_momentum, 3),
            "yes_order_active": self.yes_order_active,
            "no_order_active": self.no_order_active,
            "yes_order_price": round(self.yes_order_price, 4),
            "no_order_price": round(self.no_order_price, 4),
            "yes_ev": round(self.yes_ev, 3),
            "no_ev": round(self.no_ev, 3),
            "market_yes_ask": round(self.market_yes_ask, 4) if self.market_yes_ask is not None else None,
            "market_no_ask": round(self.market_no_ask, 4) if self.market_no_ask is not None else None,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "stop_losses": self.stop_losses,
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate * 100, 1),
            "rolling_win_rate": round(self.rolling_win_rate * 100, 1),
            "sharpe_ratio": round(self.sharpe_ratio, 2) if self.sharpe_ratio is not None else None,
            "max_drawdown": round(self.max_drawdown, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor is not None else None,
            "consecutive_losses": self.consecutive_losses,
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "recent_trades": [
                {
                    "time": t.timestamp,
                    "side": t.side,
                    "price": t.entry_price,
                    "signal": round(t.p_signal * 100, 1),
                    "confidence": round(t.confidence, 2),
                    "result": "WIN" if t.won else "LOSS",
                    "pnl": round(t.pnl, 2),
                    "exit_type": t.exit_type,
                }
                for t in self.recent_trades[-20:]
            ],
            "last_update": self.last_update,
        }


# Global singleton
state = BotState()
