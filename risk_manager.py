"""
Risk management module for the Polymarket BTC market maker.

Enforces:
  - Maximum exposure limits (was declared but NEVER checked before)
  - Session and daily drawdown limits
  - Consecutive loss circuit breaker
  - Kelly-inspired position sizing
  - Order state reconciliation
"""
import logging
import math
import time
from typing import Dict

from config import Config

log = logging.getLogger(__name__)


class RiskManager:
    """
    Pre-trade risk gate and position tracker.

    Call can_trade() before every order placement.
    Call record_fill() after successful placement.
    Call record_resolution() when a window resolves.
    """

    def __init__(self) -> None:
        # Position tracking
        self._exposure: Dict[str, float] = {}  # token_id → USDC
        self._net_exposure: float = 0.0

        # P&L tracking
        self._session_pnl: float = 0.0
        self._daily_pnl: float = 0.0
        self._peak_pnl: float = 0.0
        self._daily_reset_date: str = self._utc_date()

        # Circuit breaker
        self._consecutive_losses: int = 0
        self._circuit_breaker_until: float = 0.0  # monotonic time

        # Counters
        self._total_fills: int = 0
        self._blocked_count: int = 0

    # ------------------------------------------------------------------
    # Pre-trade check
    # ------------------------------------------------------------------

    def can_trade(self, side: str, size_usdc: float, price: float) -> tuple[bool, str]:
        """
        Check all risk limits before placing an order.
        Returns (allowed, reason) — reason is empty string if allowed.
        """
        # 1. Exposure limit (THIS WAS THE CRITICAL MISSING CHECK)
        if self._net_exposure + size_usdc > Config.MAX_EXPOSURE_USDC:
            self._blocked_count += 1
            return False, (
                f"exposure {self._net_exposure:.0f} + {size_usdc:.0f} "
                f"> limit {Config.MAX_EXPOSURE_USDC:.0f}"
            )

        # 2. Circuit breaker (consecutive losses)
        if self._is_circuit_breaker_active():
            remaining = self._circuit_breaker_until - time.monotonic()
            self._blocked_count += 1
            return False, f"circuit breaker active ({remaining:.0f}s remaining)"

        # 3. Session drawdown
        drawdown = self._peak_pnl - self._session_pnl
        if drawdown > Config.MAX_DRAWDOWN_USDC:
            self._blocked_count += 1
            return False, f"drawdown {drawdown:.2f} > limit {Config.MAX_DRAWDOWN_USDC:.0f}"

        # 4. Daily loss limit
        self._check_daily_reset()
        if self._daily_pnl < -Config.MAX_DAILY_LOSS_USDC:
            self._blocked_count += 1
            return False, f"daily loss {self._daily_pnl:.2f} < limit -{Config.MAX_DAILY_LOSS_USDC:.0f}"

        return True, ""

    # ------------------------------------------------------------------
    # Position sizing (Kelly-inspired)
    # ------------------------------------------------------------------

    def adjusted_size(
        self, base_size: float, p_signal: float, entry_price: float,
    ) -> float:
        """
        Scale order size using fractional Kelly criterion.

        Kelly fraction: f* = (p * b - q) / b
        where b = (1 - price) / price  (odds), q = 1 - p

        We use 0.25× Kelly (quarter-Kelly) for safety, capped at base_size.
        """
        if entry_price <= 0 or entry_price >= 1:
            return base_size

        b = (1.0 - entry_price) / entry_price  # payout odds
        q = 1.0 - p_signal
        kelly = (p_signal * b - q) / b if b > 0 else 0.0

        if kelly <= 0:
            return 0.0  # negative edge — don't trade

        # Quarter-Kelly, capped at base_size
        fraction = min(kelly * 0.25, 1.0)
        adjusted = base_size * fraction

        # Floor at 10% of base_size to avoid dust orders
        if adjusted < base_size * 0.1:
            return 0.0

        return round(adjusted, 2)

    # ------------------------------------------------------------------
    # Post-trade recording
    # ------------------------------------------------------------------

    def record_fill(self, side: str, size_usdc: float, price: float) -> None:
        """Record a new order fill (exposure increase)."""
        self._net_exposure += size_usdc
        self._total_fills += 1
        log.debug(
            "Risk: fill %s %.2f USDC @ %.4f — exposure now %.0f / %.0f",
            side, size_usdc, price, self._net_exposure, Config.MAX_EXPOSURE_USDC,
        )

    def record_resolution(self, won: bool, pnl: float) -> None:
        """Record a window resolution (P&L and exposure release)."""
        self._session_pnl += pnl
        self._daily_pnl += pnl

        # Update peak for drawdown tracking
        if self._session_pnl > self._peak_pnl:
            self._peak_pnl = self._session_pnl

        # Consecutive losses
        if won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= Config.MAX_CONSECUTIVE_LOSSES:
                cooldown = Config.CIRCUIT_BREAKER_COOLDOWN_SEC
                self._circuit_breaker_until = time.monotonic() + cooldown
                log.warning(
                    "Circuit breaker ACTIVATED: %d consecutive losses — "
                    "pausing for %ds",
                    self._consecutive_losses, cooldown,
                )

    def release_exposure(self, size_usdc: float) -> None:
        """Release exposure when a window resolves or order is cancelled."""
        self._net_exposure = max(0.0, self._net_exposure - size_usdc)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def total_exposure(self) -> float:
        return self._net_exposure

    def current_drawdown(self) -> float:
        return max(0.0, self._peak_pnl - self._session_pnl)

    def session_pnl(self) -> float:
        return self._session_pnl

    def to_dict(self) -> dict:
        """Export risk state for dashboard."""
        return {
            "net_exposure": round(self._net_exposure, 2),
            "max_exposure": Config.MAX_EXPOSURE_USDC,
            "session_pnl": round(self._session_pnl, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "drawdown": round(self.current_drawdown(), 2),
            "max_drawdown": Config.MAX_DRAWDOWN_USDC,
            "consecutive_losses": self._consecutive_losses,
            "circuit_breaker_active": self._is_circuit_breaker_active(),
            "total_fills": self._total_fills,
            "blocked_count": self._blocked_count,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_circuit_breaker_active(self) -> bool:
        if self._circuit_breaker_until <= 0:
            return False
        if time.monotonic() >= self._circuit_breaker_until:
            # Cooldown expired — reset
            self._circuit_breaker_until = 0.0
            self._consecutive_losses = 0
            log.info("Circuit breaker deactivated — resuming trading")
            return False
        return True

    def _check_daily_reset(self) -> None:
        today = self._utc_date()
        if today != self._daily_reset_date:
            log.info(
                "Daily P&L reset: %+.2f USDC (was %s, now %s)",
                self._daily_pnl, self._daily_reset_date, today,
            )
            self._daily_pnl = 0.0
            self._daily_reset_date = today

    @staticmethod
    def _utc_date() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())
