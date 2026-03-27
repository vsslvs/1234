"""Tests for risk management module."""
import time

import pytest

from risk_manager import RiskManager
from config import Config


class TestExposureLimits:
    """Tests for position exposure enforcement."""

    def test_allows_trade_within_limit(self, risk_manager):
        """Should allow trade when within exposure limit."""
        allowed, reason = risk_manager.can_trade("YES", 50.0, 0.92)
        assert allowed
        assert reason == ""

    def test_blocks_trade_exceeding_limit(self, risk_manager):
        """Should block trade that would exceed MAX_EXPOSURE_USDC."""
        # Fill up to near limit
        risk_manager.record_fill("YES", 480.0, 0.92)
        allowed, reason = risk_manager.can_trade("YES", 50.0, 0.92)
        assert not allowed
        assert "exposure" in reason

    def test_allows_after_exposure_release(self, risk_manager):
        """Should allow trade after exposure is released."""
        risk_manager.record_fill("YES", 480.0, 0.92)
        risk_manager.release_exposure(200.0)
        allowed, _ = risk_manager.can_trade("YES", 50.0, 0.92)
        assert allowed

    def test_exposure_tracking(self, risk_manager):
        """Total exposure should accumulate correctly."""
        risk_manager.record_fill("YES", 50.0, 0.92)
        risk_manager.record_fill("NO", 30.0, 0.92)
        assert risk_manager.total_exposure() == 80.0

    def test_release_never_goes_negative(self, risk_manager):
        """Releasing more than current exposure should floor at 0."""
        risk_manager.record_fill("YES", 50.0, 0.92)
        risk_manager.release_exposure(100.0)
        assert risk_manager.total_exposure() == 0.0


class TestDrawdownLimits:
    """Tests for session drawdown enforcement."""

    def test_no_drawdown_initially(self, risk_manager):
        assert risk_manager.current_drawdown() == 0.0

    def test_drawdown_after_losses(self, risk_manager):
        """Drawdown should increase after consecutive losses."""
        risk_manager.record_resolution(True, 4.0)   # win: peak = 4
        risk_manager.record_resolution(False, -50.0) # loss: pnl = -46
        assert risk_manager.current_drawdown() == 50.0  # peak(4) - current(-46)

    def test_blocks_after_max_drawdown(self, risk_manager):
        """Should block trading after max drawdown exceeded."""
        # Simulate large loss exceeding MAX_DRAWDOWN_USDC (100)
        risk_manager.record_resolution(False, -50.0)
        risk_manager.record_resolution(False, -50.0)
        risk_manager.record_resolution(False, -50.0)
        allowed, reason = risk_manager.can_trade("YES", 50.0, 0.92)
        assert not allowed
        assert "drawdown" in reason


class TestCircuitBreaker:
    """Tests for consecutive loss circuit breaker."""

    def test_no_breaker_on_wins(self, risk_manager):
        """Circuit breaker should not activate on wins."""
        for _ in range(10):
            risk_manager.record_resolution(True, 4.0)
        assert not risk_manager._is_circuit_breaker_active()

    def test_breaker_after_consecutive_losses(self, risk_manager):
        """Circuit breaker should activate after MAX_CONSECUTIVE_LOSSES."""
        for _ in range(Config.MAX_CONSECUTIVE_LOSSES):
            risk_manager.record_resolution(False, -50.0)
        assert risk_manager._is_circuit_breaker_active()

    def test_breaker_blocks_trading(self, risk_manager):
        """Active circuit breaker should block all trades."""
        for _ in range(Config.MAX_CONSECUTIVE_LOSSES):
            risk_manager.record_resolution(False, -50.0)
        allowed, reason = risk_manager.can_trade("YES", 50.0, 0.92)
        assert not allowed
        assert "circuit breaker" in reason

    def test_win_resets_counter(self, risk_manager):
        """A win should reset the consecutive loss counter."""
        for _ in range(Config.MAX_CONSECUTIVE_LOSSES - 1):
            risk_manager.record_resolution(False, -50.0)
        risk_manager.record_resolution(True, 4.0)
        assert risk_manager._consecutive_losses == 0


class TestKellySizing:
    """Tests for Kelly criterion position sizing."""

    def test_high_confidence_near_full_size(self, risk_manager):
        """Very high signal → near full position size."""
        size = risk_manager.adjusted_size(50.0, 0.99, 0.92)
        assert size > 0
        assert size <= 50.0

    def test_marginal_signal_small_size(self, risk_manager):
        """Marginal signal (just above breakeven) → small size."""
        size = risk_manager.adjusted_size(50.0, 0.93, 0.92)
        assert size < 50.0

    def test_below_breakeven_returns_zero(self, risk_manager):
        """Signal below breakeven → zero size (don't trade)."""
        size = risk_manager.adjusted_size(50.0, 0.90, 0.92)
        assert size == 0.0

    def test_exactly_breakeven_returns_zero(self, risk_manager):
        """Signal exactly at breakeven → zero (no edge)."""
        size = risk_manager.adjusted_size(50.0, 0.92, 0.92)
        assert size == 0.0

    def test_invalid_price_returns_base(self, risk_manager):
        """Invalid prices → return base size."""
        assert risk_manager.adjusted_size(50.0, 0.95, 0.0) == 50.0
        assert risk_manager.adjusted_size(50.0, 0.95, 1.0) == 50.0


class TestDailyLoss:
    """Tests for daily loss limit."""

    def test_daily_pnl_tracked(self, risk_manager):
        risk_manager.record_resolution(False, -50.0)
        assert risk_manager._daily_pnl == -50.0

    def test_blocks_after_daily_limit(self, risk_manager):
        """Should block after daily loss exceeds limit."""
        # First push peak PnL high to avoid drawdown limit triggering first
        risk_manager.record_resolution(True, 250.0)  # peak = 250
        # Now lose enough to exceed daily limit (200) but stay within drawdown (100)
        # peak=250, after losses: pnl = 250 - 210 = 40, drawdown = 250-40 = 210 > 100
        # Hmm, need to keep drawdown under 100 while daily loss > 200...
        # Actually the daily_pnl tracks wins AND losses within today.
        # Let's just ensure the daily_pnl check happens:
        # Reset peak to avoid drawdown check: lose small amounts with wins in between
        # Peak stays at ~250, session_pnl stays above 150 => drawdown < 100
        # But daily_pnl accumulates all: +250 -25 +1 -25 +1 ...
        # daily = 250 - 25*N + 1*(N-1); want daily < -200 and drawdown < 100
        # Alternative: manipulate _daily_pnl directly for unit test
        risk_manager._daily_pnl = -201.0
        allowed, reason = risk_manager.can_trade("YES", 50.0, 0.92)
        assert not allowed
        assert "daily loss" in reason


class TestToDict:
    """Tests for dashboard export."""

    def test_to_dict_keys(self, risk_manager):
        d = risk_manager.to_dict()
        assert "net_exposure" in d
        assert "session_pnl" in d
        assert "drawdown" in d
        assert "circuit_breaker_active" in d
        assert "consecutive_losses" in d
