"""
Win-rate and P&L statistics tracker for the Polymarket BTC market maker.

Tracks per-window trade outcomes and computes:
  - Observed win rate (overall and rolling)
  - Expected value per trade
  - Session P&L estimate

Resolution approximation
------------------------
Actual Polymarket resolution uses the Chainlink BTC/USD price feed.
We approximate it with the Binance mid-price at window close.  BTC prices
on Binance and Chainlink are highly correlated on 5-minute scales (typical
delta < 0.05%), so this approximation introduces negligible error for
statistics purposes.

Signal model
------------
The bot uses a random-walk CDF signal:

    p_up = Phi( ret / sigma_remaining )

where sigma_remaining = sigma_5m * sqrt(seconds_to_close / 300).
This gives time-adjusted probabilities that increase confidence as the
window approaches close.  See market_calculator.py for details.

Break-even win rate = entry_price  (derivation: p = price).
With two-sided quoting, both sides fill => guaranteed profit = 1 - cost.
"""
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

log = logging.getLogger(__name__)

# Number of recent trades for rolling win-rate display
ROLLING_WINDOW = 50


@dataclass
class TradeRecord:
    """One resolved trade outcome."""
    window_start: int
    side:         str    # "YES" (Up token) or "NO" (Down token)
    entry_price:  float  # price we paid, e.g. 0.47
    size_usdc:    float  # USDC staked
    p_signal:     float  # p_up probability at entry
    won:          bool   # True if BTC closed in our predicted direction
    pnl:          float  # approximate realised P&L in USDC


class BotStats:
    """
    Tracks win rate and P&L statistics for a single bot session.

    Call record_trade() on every resolved window where we had a fill.
    Call log_summary() periodically or on shutdown.
    """

    def __init__(self) -> None:
        self._trades: Deque[TradeRecord] = deque(maxlen=1000)
        self._wins:      int   = 0
        self._losses:    int   = 0
        self._total_pnl: float = 0.0
        self._session_start: float = time.time()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_trade(
        self,
        *,
        window_start: int,
        side:         str,
        entry_price:  float,
        size_usdc:    float,
        p_signal:     float,
        won:          bool,
        pnl_override: Optional[float] = None,
    ) -> None:
        """Record one resolved trade and log the outcome.

        If pnl_override is set (e.g. stop-loss exit), use that instead of
        the standard binary payout calculation.
        """
        shares = size_usdc / entry_price
        if pnl_override is not None:
            pnl = pnl_override
        else:
            pnl = shares * (1.0 - entry_price) if won else -size_usdc
        record = TradeRecord(
            window_start=window_start,
            side=side,
            entry_price=entry_price,
            size_usdc=size_usdc,
            p_signal=p_signal,
            won=won,
            pnl=pnl,
        )
        self._trades.append(record)
        if won:
            self._wins += 1
        else:
            self._losses += 1
        self._total_pnl += pnl
        log.info(
            "Trade  window=%d  side=%-3s  @%.4f  signal=%.1f%%  ->  %-4s  "
            "P&L=%+.2f USDC  (session: %d/%d  rate=%.1f%%)",
            window_start, side, entry_price, p_signal * 100,
            "WIN" if won else "LOSS", pnl,
            self._wins, self.total_trades,
            (self.win_rate or 0.0) * 100,
        )

    # ------------------------------------------------------------------
    # Observed statistics
    # ------------------------------------------------------------------

    @property
    def total_trades(self) -> int:
        return self._wins + self._losses

    @property
    def total_pnl(self) -> float:
        return self._total_pnl

    @property
    def win_rate(self) -> Optional[float]:
        """Overall win rate for the current session."""
        return self._wins / self.total_trades if self.total_trades else None

    def rolling_win_rate(self, n: int = ROLLING_WINDOW) -> Optional[float]:
        """Win rate over the most recent n resolved trades."""
        recent = list(self._trades)[-n:]
        if not recent:
            return None
        return sum(1 for t in recent if t.won) / len(recent)

    @property
    def avg_pnl_per_trade(self) -> Optional[float]:
        return self._total_pnl / self.total_trades if self.total_trades else None

    @property
    def avg_entry_price(self) -> Optional[float]:
        """Average entry price across all trades."""
        if not self._trades:
            return None
        return sum(t.entry_price for t in self._trades) / len(self._trades)

    @staticmethod
    def break_even_win_rate(entry_price: float) -> float:
        """Minimum win rate for zero EV: p_break_even = entry_price."""
        return entry_price

    @staticmethod
    def theoretical_ev_per_trade(
        win_rate:    float,
        entry_price: float,
        size_usdc:   float,
    ) -> float:
        """
        Expected P&L per trade in USDC.

            EV = p * (size/price) * (1 - price) + (1-p) * (-size)
        """
        shares = size_usdc / entry_price
        return win_rate * shares * (1.0 - entry_price) + (1.0 - win_rate) * (-size_usdc)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def log_summary(self) -> None:
        """Log a formatted block with observed statistics."""
        uptime_h = (time.time() - self._session_start) / 3600

        if self.total_trades == 0:
            log.info(
                "--- Bot Statistics (uptime %.1fh) ---\n"
                "  No trades recorded yet.\n"
                "  Two-sided spread quoting: bid = fair - spread\n"
                "  Fills: pending until market ask <= our bid",
                uptime_h,
            )
            return

        wr_actual  = self.win_rate or 0.0
        wr_rolling = self.rolling_win_rate() or 0.0
        ev_actual  = self.avg_pnl_per_trade or 0.0
        avg_entry  = self.avg_entry_price or 0.0

        # Count two-sided windows (both YES and NO filled in same window)
        from collections import Counter
        window_sides: Counter = Counter()
        for t in self._trades:
            window_sides[t.window_start] += 1
        two_sided = sum(1 for c in window_sides.values() if c >= 2)

        log.info(
            "--- Bot Statistics (uptime %.1fh, %d trades) ---\n"
            "  Wins / Losses          : %d / %d\n"
            "  Win rate  (actual)     : %.1f%%\n"
            "  Win rate  (roll-%d)    : %.1f%%\n"
            "  Avg entry price        : %.4f  (break-even: %.1f%%)\n"
            "  EV/trade  (actual)     : %+.3f USDC\n"
            "  Total P&L              : %+.2f USDC\n"
            "  Two-sided fills        : %d windows",
            uptime_h, self.total_trades,
            self._wins, self._losses,
            wr_actual * 100,
            ROLLING_WINDOW, wr_rolling * 100,
            avg_entry, avg_entry * 100,
            ev_actual,
            self._total_pnl,
            two_sided,
        )
