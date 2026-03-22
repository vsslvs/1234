"""
Win-rate and P&L statistics tracker for the Polymarket BTC market maker.

Tracks per-window trade outcomes and computes:
  - Observed win rate (overall and rolling)
  - Theoretical win rate from the calibrated logistic signal model
  - Expected value per trade
  - Session P&L estimate

Resolution approximation
------------------------
Actual Polymarket resolution uses the Chainlink BTC/USD price feed.
We approximate it with the Binance mid-price at window close.  BTC prices
on Binance and Chainlink are highly correlated on 5-minute scales (typical
delta < 0.05%), so this approximation introduces negligible error for
statistics purposes.

Calibration math
----------------
The logistic signal p_up = 1 / (1 + exp(-k × ret)) maps the BTC return
observed during the window to a win probability.  We calibrate k so that
the logistic p at the entry threshold matches the true statistical
probability derived from a random-walk model:

    P(win | ret_at_entry) = Φ( ret_at_entry / σ_remaining )

where:
    σ_remaining = σ₅ × √(ENTRY_WINDOW_SEC / MARKET_WINDOW_SEC)
    σ₅          ≈ 0.22%  (BTC annualised-vol 60% → 5-min window)

At entry threshold (P_UP_THRESHOLD = 0.94):
    ret_min     = ln(0.94 / 0.06) / k
    True P      = Φ(ret_min / σ_remaining)

For exact calibration at σ₅ = 0.22%:  k_exact   = 4 421
For safety margin at σ₅ = 0.50%:       k_safe    = 2 000  ← used
(VOLATILITY_GATE_BPS = 200 ensures we skip windows where σ₅ > 0.70%,
 preserving positive EV across all traded conditions.)

Theoretical win rates with k = 2 000, threshold = 0.94:
    σ₅ = 0.22%  → P = 99.97%  (typical day)
    σ₅ = 0.50%  → P = 93.6%   (high-vol day, still > break-even 92%)
    σ₅ > 0.70%  → not traded  (blocked by volatility gate)

Break-even win rate = TARGET_PRICE_YES = 0.92  (derivation: p = price).
"""
import logging
import math
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Deque, Optional

log = logging.getLogger(__name__)

# Number of recent trades for rolling win-rate display
ROLLING_WINDOW = 50


@dataclass
class TradeRecord:
    """One resolved trade outcome."""
    window_start: int
    side:         str    # "YES" (Up token) or "NO" (Down token)
    entry_price:  float  # price we paid, e.g. 0.92
    size_usdc:    float  # USDC staked
    p_signal:     float  # logistic signal probability at entry
    won:          bool   # True if BTC closed in our predicted direction
    pnl:          float  # approximate realised P&L in USDC


class BotStats:
    """
    Tracks win rate and P&L statistics for a single bot session.

    Call record_trade() on every resolved window where we had an order.
    Call log_summary() periodically or on shutdown.
    """

    # Default BTC 5-minute return volatility used for theoretical calculations.
    # Derived from annualised vol 60%: σ₅ = 60% / sqrt(252 × 24 × 12) ≈ 0.22%.
    SIGMA_5M_DEFAULT: float = 0.0022

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
    ) -> None:
        """Record one resolved trade and log the outcome."""
        shares = size_usdc / entry_price
        pnl    = shares * (1.0 - entry_price) if won else -size_usdc
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
            "Trade  window=%d  side=%-3s  @%.4f  signal=%.1f%%  →  %-4s  "
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

    # ------------------------------------------------------------------
    # Theoretical / analytical statistics
    # ------------------------------------------------------------------

    @staticmethod
    def theoretical_win_rate(
        k:                 float,
        threshold:         float,
        entry_window_sec:  int,
        market_window_sec: int,
        sigma_5m:          float = SIGMA_5M_DEFAULT,
    ) -> float:
        """
        P(BTC closes in our direction | we entered at p ≥ threshold).

        Random-walk model:
            ret_min    = logit(threshold) / k
                       = ln(threshold / (1−threshold)) / k
            σ_remaining = σ₅ × √(entry_window_sec / market_window_sec)
            P(win)     = Φ(ret_min / σ_remaining)

        Uses math.erf for the standard normal CDF (no scipy required):
            Φ(z) = 0.5 × (1 + erf(z / √2))
        """
        ret_min   = math.log(threshold / (1.0 - threshold)) / k
        sigma_rem = sigma_5m * math.sqrt(entry_window_sec / market_window_sec)
        z         = ret_min / sigma_rem
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    @staticmethod
    def theoretical_ev_per_trade(
        win_rate:    float,
        entry_price: float,
        size_usdc:   float,
    ) -> float:
        """
        Expected P&L per trade in USDC.

            EV = p × (size/price) × (1 − price)  +  (1−p) × (−size)
               = p × shares × profit_per_share − (1−p) × size
        """
        shares = size_usdc / entry_price
        return win_rate * shares * (1.0 - entry_price) + (1.0 - win_rate) * (-size_usdc)

    @staticmethod
    def break_even_win_rate(entry_price: float) -> float:
        """
        Minimum win rate for zero EV.

        Derivation:
            p × (1/price − 1) × size = (1−p) × size
            p × (1 − price)  = (1−p) × price
            p = price
        """
        return entry_price

    @staticmethod
    def entry_frequency(
        k:                 float,
        threshold:         float,
        market_window_sec: int,
        entry_window_sec:  int,
        sigma_5m:          float = SIGMA_5M_DEFAULT,
    ) -> float:
        """
        Fraction of 5-minute windows expected to trigger at least one side.

        At the entry moment (entry_window_sec before close), the elapsed
        return follows N(0, σ_elapsed²) where
            σ_elapsed = σ₅ × √((market_window_sec − entry_window_sec)
                                / market_window_sec)
        P(trigger one side) = 2 × P(ret > ret_threshold)
                            = 2 × (1 − Φ(ret_threshold / σ_elapsed))
        Capped at 1.0.
        """
        elapsed_sec = market_window_sec - entry_window_sec
        sigma_el    = sigma_5m * math.sqrt(elapsed_sec / market_window_sec)
        ret_min     = math.log(threshold / (1.0 - threshold)) / k
        z           = ret_min / sigma_el
        p_one_side  = 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))
        return min(2.0 * p_one_side, 1.0)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Export current statistics as a plain dict for the dashboard."""
        return {
            "wins": self._wins,
            "losses": self._losses,
            "total_pnl": round(self._total_pnl, 2),
            "win_rate": self.win_rate,
            "rolling_win_rate": self.rolling_win_rate(),
            "avg_pnl": round(self.avg_pnl_per_trade, 2) if self.avg_pnl_per_trade else None,
            "total_trades": self.total_trades,
            "recent_trades": [asdict(t) for t in list(self._trades)[-20:]],
        }

    def log_summary(
        self,
        k:                 float,
        entry_window_sec:  int,
        market_window_sec: int,
        threshold:         float = 0.94,
        entry_price:       float = 0.92,
        size_usdc:         float = 50.0,
    ) -> None:
        """Log a formatted block with observed and theoretical statistics."""
        uptime_h = (time.time() - self._session_start) / 3600

        wr_theory    = self.theoretical_win_rate(
            k, threshold, entry_window_sec, market_window_sec
        )
        wr_breakeven = self.break_even_win_rate(entry_price)
        ev_theory    = self.theoretical_ev_per_trade(wr_theory, entry_price, size_usdc)
        freq         = self.entry_frequency(
            k, threshold, market_window_sec, entry_window_sec
        )
        trades_per_day = round(freq * 288)   # 288 five-minute windows per day

        if self.total_trades == 0:
            log.info(
                "┌─ Bot Statistics (uptime %.1fh) ──────────────────────────────┐\n"
                "│  No trades recorded yet.\n"
                "│  Theoretical win rate : %.2f%%   Break-even : %.2f%%\n"
                "│  EV per trade         : %+.3f USDC on %.0f USDC stake\n"
                "│  Expected frequency   : %.0f%%/window  ≈ %d trades/day\n"
                "└──────────────────────────────────────────────────────────────┘",
                uptime_h,
                wr_theory * 100, wr_breakeven * 100,
                ev_theory, size_usdc,
                freq * 100, trades_per_day,
            )
            return

        wr_actual  = self.win_rate or 0.0
        wr_rolling = self.rolling_win_rate() or 0.0
        ev_actual  = self.avg_pnl_per_trade or 0.0

        log.info(
            "┌─ Bot Statistics (uptime %.1fh,  %d trades) ───────────────────────┐\n"
            "│  Wins / Losses          : %d / %d\n"
            "│  Win rate  (actual)     : %.2f%%\n"
            "│  Win rate  (roll-%d)    : %.2f%%\n"
            "│  Win rate  (theory)     : %.2f%%   Break-even : %.2f%%\n"
            "│  EV/trade  (theory)     : %+.3f USDC\n"
            "│  EV/trade  (actual)     : %+.3f USDC\n"
            "│  Total P&L              : %+.2f USDC\n"
            "│  Expected frequency     : %.0f%%/window  ≈ %d trades/day\n"
            "└────────────────────────────────────────────────────────────────────┘",
            uptime_h, self.total_trades,
            self._wins, self._losses,
            wr_actual * 100,
            ROLLING_WINDOW, wr_rolling * 100,
            wr_theory * 100, wr_breakeven * 100,
            ev_theory,
            ev_actual,
            self._total_pnl,
            freq * 100, trades_per_day,
        )
