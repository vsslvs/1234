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

    # Current window
    window_start: int = 0
    window_end: int = 0
    seconds_to_close: float = 0.0
    phase: str = "initializing"  # "waiting", "entry", "exit", "vol_skip"

    # Orders
    yes_order_active: bool = False
    no_order_active: bool = False
    yes_order_price: float = 0.0
    no_order_price: float = 0.0

    # Stats
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    rolling_win_rate: float = 0.0

    # Recent trades (last 20)
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
            "window_start": self.window_start,
            "window_end": self.window_end,
            "seconds_to_close": round(self.seconds_to_close, 1),
            "phase": self.phase,
            "yes_order_active": self.yes_order_active,
            "no_order_active": self.no_order_active,
            "yes_order_price": round(self.yes_order_price, 4),
            "no_order_price": round(self.no_order_price, 4),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate * 100, 1),
            "rolling_win_rate": round(self.rolling_win_rate * 100, 1),
            "recent_trades": [
                {
                    "time": t.timestamp,
                    "side": t.side,
                    "price": t.entry_price,
                    "signal": round(t.p_signal * 100, 1),
                    "result": "WIN" if t.won else "LOSS",
                    "pnl": round(t.pnl, 2),
                }
                for t in self.recent_trades[-20:]
            ],
            "last_update": self.last_update,
        }


# Global singleton
state = BotState()
