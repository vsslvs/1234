"""
Advanced statistics tracker for the Polymarket BTC market maker.

Tracks per-window trade outcomes and computes:
  - Observed win rate (overall and rolling)
  - Expected value per trade
  - Session P&L with fee tracking
  - Sharpe ratio (annualized from 5-minute windows)
  - Maximum drawdown (peak-to-trough)
  - Consecutive win/loss streaks
  - Per-side (YES/NO) performance breakdown
  - Signal calibration (predicted vs actual win rate)

Resolution approximation
------------------------
Actual Polymarket resolution uses the Chainlink BTC/USD price feed.
We approximate it with the Binance mid-price at window close.  BTC prices
on Binance and Chainlink are highly correlated on 5-minute scales (typical
delta < 0.05%), so this approximation introduces negligible error.
"""
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

ROLLING_WINDOW = 50


@dataclass
class TradeRecord:
    """One resolved trade outcome."""
    window_start: int
    side:         str    # "YES" or "NO"
    entry_price:  float
    size_usdc:    float
    p_signal:     float  # p_up probability at entry
    won:          bool
    pnl:          float
    fee:          float
    confidence:   float = 0.0  # signal confidence at entry


class BotStats:
    """
    Comprehensive statistics tracker for a single bot session.

    Call record_trade() on every resolved window.
    Call log_summary() periodically or on shutdown.
    """

    def __init__(self) -> None:
        self._trades: Deque[TradeRecord] = deque(maxlen=1000)
        self._wins:      int   = 0
        self._losses:    int   = 0
        self._total_pnl: float = 0.0
        self._total_fees: float = 0.0
        self._total_maker_rebate_est: float = 0.0
        self._session_start: float = time.time()

        # Drawdown tracking
        self._peak_pnl: float = 0.0
        self._max_drawdown: float = 0.0  # most negative drawdown observed

        # Streak tracking
        self._current_streak: int = 0    # positive = wins, negative = losses
        self._max_win_streak: int = 0
        self._max_loss_streak: int = 0
        self._consecutive_losses: int = 0  # current consecutive loss count

        # Sharpe components (per-trade returns for variance calc)
        self._returns: Deque[float] = deque(maxlen=1000)

        # Per-side tracking
        self._yes_wins: int = 0
        self._yes_losses: int = 0
        self._no_wins: int = 0
        self._no_losses: int = 0

        # Signal calibration buckets: bucket_key → (predicted_sum, actual_wins, count)
        self._calibration: Dict[int, List[float]] = {}  # bucket 0-9 → [sum_p, wins, count]

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
        fee:          float = 0.0,
        confidence:   float = 0.0,
    ) -> None:
        """Record one resolved trade and update all statistics."""
        shares = size_usdc / entry_price if entry_price > 0 else 0
        if pnl_override is not None:
            pnl = pnl_override
        else:
            pnl = shares * (1.0 - entry_price) - fee if won else -size_usdc

        record = TradeRecord(
            window_start=window_start,
            side=side,
            entry_price=entry_price,
            size_usdc=size_usdc,
            p_signal=p_signal,
            won=won,
            pnl=pnl,
            fee=fee,
            confidence=confidence,
        )
        self._trades.append(record)

        # Win/loss counters
        if won:
            self._wins += 1
        else:
            self._losses += 1

        # Per-side tracking
        if side == "YES":
            if won:
                self._yes_wins += 1
            else:
                self._yes_losses += 1
        else:
            if won:
                self._no_wins += 1
            else:
                self._no_losses += 1

        # P&L and fees
        self._total_pnl += pnl
        self._total_fees += fee

        # Maker rebate estimate
        from config import Config
        self._total_maker_rebate_est += fee * Config.MAKER_REBATE_PCT

        # Drawdown tracking
        if self._total_pnl > self._peak_pnl:
            self._peak_pnl = self._total_pnl
        current_dd = self._total_pnl - self._peak_pnl
        if current_dd < self._max_drawdown:
            self._max_drawdown = current_dd

        # Streak tracking
        if won:
            self._consecutive_losses = 0
            if self._current_streak > 0:
                self._current_streak += 1
            else:
                self._current_streak = 1
            self._max_win_streak = max(self._max_win_streak, self._current_streak)
        else:
            self._consecutive_losses += 1
            if self._current_streak < 0:
                self._current_streak -= 1
            else:
                self._current_streak = -1
            self._max_loss_streak = max(self._max_loss_streak, abs(self._current_streak))

        # Sharpe: track per-trade return rate
        if size_usdc > 0:
            self._returns.append(pnl / size_usdc)

        # Signal calibration: bucket by predicted probability in 10% bins
        p_for_side = p_signal if side == "YES" else (1.0 - p_signal)
        bucket = min(9, max(0, int(p_for_side * 10)))
        if bucket not in self._calibration:
            self._calibration[bucket] = [0.0, 0.0, 0.0]
        self._calibration[bucket][0] += p_for_side  # sum of predicted probs
        self._calibration[bucket][1] += 1.0 if won else 0.0  # actual wins
        self._calibration[bucket][2] += 1.0  # count

        log.info(
            "Trade  window=%d  side=%-3s  @%.4f  signal=%.1f%%  conf=%.2f  ->  %-4s  "
            "P&L=%+.2f USDC  fee=%.4f  (session: %d/%d  rate=%.1f%%  dd=%.2f)",
            window_start, side, entry_price, p_signal * 100, confidence,
            "WIN" if won else "LOSS", pnl, fee,
            self._wins, self.total_trades,
            (self.win_rate or 0.0) * 100,
            self._max_drawdown,
        )

    # ------------------------------------------------------------------
    # Core statistics
    # ------------------------------------------------------------------

    @property
    def total_trades(self) -> int:
        return self._wins + self._losses

    @property
    def total_pnl(self) -> float:
        return self._total_pnl

    @property
    def win_rate(self) -> Optional[float]:
        return self._wins / self.total_trades if self.total_trades else None

    def rolling_win_rate(self, n: int = ROLLING_WINDOW) -> Optional[float]:
        recent = list(self._trades)[-n:]
        if not recent:
            return None
        return sum(1 for t in recent if t.won) / len(recent)

    @property
    def avg_pnl_per_trade(self) -> Optional[float]:
        return self._total_pnl / self.total_trades if self.total_trades else None

    @property
    def avg_entry_price(self) -> Optional[float]:
        if not self._trades:
            return None
        return sum(t.entry_price for t in self._trades) / len(self._trades)

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def max_drawdown(self) -> float:
        return self._max_drawdown

    @property
    def current_drawdown(self) -> float:
        """Current drawdown from peak P&L."""
        return self._total_pnl - self._peak_pnl

    # ------------------------------------------------------------------
    # Advanced metrics
    # ------------------------------------------------------------------

    @property
    def sharpe_ratio(self) -> Optional[float]:
        """
        Annualized Sharpe ratio from per-trade returns.

        Since each trade spans ~5 minutes, annualization factor is:
        √(288 trades/day × 365 days) ≈ √105,120 ≈ 324.2

        Returns None if insufficient data for meaningful estimate.
        """
        if len(self._returns) < 20:
            return None
        returns = list(self._returns)
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        if var_r < 1e-12:
            return None
        std_r = math.sqrt(var_r)
        # Annualize based on ACTUAL trades per day, not theoretical 288.
        # Estimate actual trades/day from session data:
        uptime_days = max(1/288, (time.time() - self._session_start) / 86400)
        trades_per_day = len(returns) / uptime_days
        annualization = math.sqrt(trades_per_day * 365)
        return (mean_r / std_r) * annualization

    @property
    def profit_factor(self) -> Optional[float]:
        """
        Gross profit / gross loss. > 1.0 means profitable.
        """
        gross_profit = sum(t.pnl for t in self._trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self._trades if t.pnl < 0))
        if gross_loss < 0.001:
            return None
        return gross_profit / gross_loss

    @property
    def avg_win_size(self) -> Optional[float]:
        """Average P&L on winning trades."""
        wins = [t.pnl for t in self._trades if t.won]
        return sum(wins) / len(wins) if wins else None

    @property
    def avg_loss_size(self) -> Optional[float]:
        """Average P&L on losing trades (negative number)."""
        losses = [t.pnl for t in self._trades if not t.won]
        return sum(losses) / len(losses) if losses else None

    @property
    def win_loss_ratio(self) -> Optional[float]:
        """Average win / |average loss|. Higher = better risk/reward."""
        avg_w = self.avg_win_size
        avg_l = self.avg_loss_size
        if avg_w is None or avg_l is None or avg_l == 0:
            return None
        return avg_w / abs(avg_l)

    def side_stats(self) -> Dict[str, Dict[str, float]]:
        """Per-side (YES/NO) win rate and P&L breakdown."""
        result = {}
        for side_name, wins, losses in [
            ("YES", self._yes_wins, self._yes_losses),
            ("NO", self._no_wins, self._no_losses),
        ]:
            total = wins + losses
            pnl = sum(t.pnl for t in self._trades if t.side == side_name)
            result[side_name] = {
                "trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": wins / total if total > 0 else 0.0,
                "pnl": pnl,
            }
        return result

    def calibration_table(self) -> List[Tuple[str, float, float, int]]:
        """
        Signal calibration: for each probability bucket, shows
        (bucket_label, avg_predicted, actual_win_rate, count).

        A well-calibrated signal should have avg_predicted ≈ actual_win_rate.
        """
        table = []
        for bucket in sorted(self._calibration.keys()):
            data = self._calibration[bucket]
            count = int(data[2])
            if count == 0:
                continue
            avg_pred = data[0] / count
            actual_wr = data[1] / count
            label = f"{bucket*10}-{(bucket+1)*10}%"
            table.append((label, avg_pred, actual_wr, count))
        return table

    @staticmethod
    def break_even_win_rate(entry_price: float) -> float:
        return entry_price

    @staticmethod
    def theoretical_ev_per_trade(
        win_rate: float, entry_price: float, size_usdc: float,
    ) -> float:
        shares = size_usdc / entry_price
        return win_rate * shares * (1.0 - entry_price) + (1.0 - win_rate) * (-size_usdc)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def log_summary(self) -> None:
        """Log comprehensive statistics block."""
        uptime_h = (time.time() - self._session_start) / 3600

        if self.total_trades == 0:
            log.info(
                "--- Bot Statistics (uptime %.1fh) ---\n"
                "  No trades recorded yet.",
                uptime_h,
            )
            return

        wr_actual  = self.win_rate or 0.0
        wr_rolling = self.rolling_win_rate() or 0.0
        ev_actual  = self.avg_pnl_per_trade or 0.0
        avg_entry  = self.avg_entry_price or 0.0
        sharpe     = self.sharpe_ratio
        pf         = self.profit_factor
        wl_ratio   = self.win_loss_ratio

        from collections import Counter
        window_sides: Counter = Counter()
        for t in self._trades:
            window_sides[t.window_start] += 1
        two_sided = sum(1 for c in window_sides.values() if c >= 2)

        # Side breakdown
        sides = self.side_stats()

        log.info(
            "--- Bot Statistics (uptime %.1fh, %d trades) ---\n"
            "  Wins / Losses          : %d / %d\n"
            "  Win rate  (actual)     : %.1f%%\n"
            "  Win rate  (roll-%d)    : %.1f%%\n"
            "  Avg entry price        : %.4f  (break-even: %.1f%%)\n"
            "  EV/trade  (actual)     : %+.3f USDC\n"
            "  Total P&L              : %+.2f USDC\n"
            "  Total fees paid        : %.4f USDC\n"
            "  Est. maker rebate      : %.4f USDC\n"
            "  Net P&L (incl rebate)  : %+.2f USDC\n"
            "  Max drawdown           : %.2f USDC\n"
            "  Sharpe ratio (ann.)    : %s\n"
            "  Profit factor          : %s\n"
            "  Win/loss ratio         : %s\n"
            "  Max win streak         : %d\n"
            "  Max loss streak        : %d\n"
            "  Two-sided fills        : %d windows\n"
            "  YES: %dW/%dL (%.1f%%) P&L=%+.2f\n"
            "  NO:  %dW/%dL (%.1f%%) P&L=%+.2f",
            uptime_h, self.total_trades,
            self._wins, self._losses,
            wr_actual * 100,
            ROLLING_WINDOW, wr_rolling * 100,
            avg_entry, avg_entry * 100,
            ev_actual,
            self._total_pnl,
            self._total_fees,
            self._total_maker_rebate_est,
            self._total_pnl + self._total_maker_rebate_est,
            self._max_drawdown,
            f"{sharpe:.2f}" if sharpe is not None else "N/A",
            f"{pf:.2f}" if pf is not None else "N/A",
            f"{wl_ratio:.2f}" if wl_ratio is not None else "N/A",
            self._max_win_streak,
            self._max_loss_streak,
            two_sided,
            sides["YES"]["wins"], sides["YES"]["losses"],
            sides["YES"]["win_rate"] * 100, sides["YES"]["pnl"],
            sides["NO"]["wins"], sides["NO"]["losses"],
            sides["NO"]["win_rate"] * 100, sides["NO"]["pnl"],
        )

        # Log calibration table
        cal = self.calibration_table()
        if cal:
            cal_lines = ["  Signal Calibration:"]
            for label, pred, actual, count in cal:
                cal_lines.append(f"    {label}: predicted={pred:.1%} actual={actual:.1%} (n={count})")
            log.info("\n".join(cal_lines))
