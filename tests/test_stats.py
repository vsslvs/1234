"""Tests for statistics calculations."""
import pytest

from stats import BotStats


class TestTheoreticalWinRate:
    """Tests for random-walk win rate model."""

    def test_default_params_high_win_rate(self, bot_stats):
        """With default params (σ=0.22%), win rate should be very high."""
        wr = bot_stats.theoretical_win_rate(
            k=2000, threshold=0.94,
            entry_window_sec=10, market_window_sec=300,
        )
        assert wr > 0.99

    def test_high_vol_lower_win_rate(self, bot_stats):
        """Higher volatility → lower (but still positive EV) win rate."""
        wr = bot_stats.theoretical_win_rate(
            k=2000, threshold=0.94,
            entry_window_sec=10, market_window_sec=300,
            sigma_5m=0.005,  # 0.50% — high vol
        )
        assert 0.90 < wr < 0.99

    def test_win_rate_bounded(self, bot_stats):
        """Win rate should always be in [0, 1]."""
        for sigma in [0.001, 0.002, 0.005, 0.01]:
            wr = bot_stats.theoretical_win_rate(
                k=2000, threshold=0.94,
                entry_window_sec=10, market_window_sec=300,
                sigma_5m=sigma,
            )
            assert 0 <= wr <= 1


class TestBreakEvenWinRate:
    def test_breakeven_equals_entry_price(self, bot_stats):
        """Break-even win rate should equal entry price."""
        assert bot_stats.break_even_win_rate(0.92) == 0.92
        assert bot_stats.break_even_win_rate(0.50) == 0.50
        assert bot_stats.break_even_win_rate(0.95) == 0.95


class TestEVPerTrade:
    def test_positive_ev_above_breakeven(self, bot_stats):
        """Win rate above breakeven → positive EV."""
        ev = bot_stats.theoretical_ev_per_trade(0.95, 0.92, 50.0)
        assert ev > 0

    def test_negative_ev_below_breakeven(self, bot_stats):
        """Win rate below breakeven → negative EV."""
        ev = bot_stats.theoretical_ev_per_trade(0.90, 0.92, 50.0)
        assert ev < 0

    def test_zero_ev_at_breakeven(self, bot_stats):
        """Win rate at exactly breakeven → ~zero EV."""
        ev = bot_stats.theoretical_ev_per_trade(0.92, 0.92, 50.0)
        assert abs(ev) < 0.01


class TestEntryFrequency:
    def test_frequency_between_zero_and_one(self, bot_stats):
        """Entry frequency should be in [0, 1]."""
        freq = bot_stats.entry_frequency(
            k=2000, threshold=0.94,
            market_window_sec=300, entry_window_sec=10,
        )
        assert 0 <= freq <= 1

    def test_lower_threshold_more_frequent(self, bot_stats):
        """Lower threshold → more frequent entries."""
        freq_high = bot_stats.entry_frequency(
            k=2000, threshold=0.97,
            market_window_sec=300, entry_window_sec=10,
        )
        freq_low = bot_stats.entry_frequency(
            k=2000, threshold=0.90,
            market_window_sec=300, entry_window_sec=10,
        )
        assert freq_low > freq_high


class TestRecordTrade:
    def test_win_recording(self, bot_stats):
        bot_stats.record_trade(
            window_start=1000, side="YES",
            entry_price=0.92, size_usdc=50.0,
            p_signal=0.95, won=True,
        )
        assert bot_stats.total_trades == 1
        assert bot_stats._wins == 1
        assert bot_stats.total_pnl > 0

    def test_loss_recording(self, bot_stats):
        bot_stats.record_trade(
            window_start=1000, side="YES",
            entry_price=0.92, size_usdc=50.0,
            p_signal=0.95, won=False,
        )
        assert bot_stats.total_trades == 1
        assert bot_stats._losses == 1
        assert bot_stats.total_pnl == -50.0

    def test_win_rate_calculation(self, bot_stats):
        for _ in range(8):
            bot_stats.record_trade(
                window_start=1000, side="YES",
                entry_price=0.92, size_usdc=50.0,
                p_signal=0.95, won=True,
            )
        for _ in range(2):
            bot_stats.record_trade(
                window_start=1000, side="YES",
                entry_price=0.92, size_usdc=50.0,
                p_signal=0.95, won=False,
            )
        assert bot_stats.win_rate == 0.8

    def test_rolling_win_rate(self, bot_stats):
        # Fill with 50 wins
        for _ in range(50):
            bot_stats.record_trade(
                window_start=1000, side="YES",
                entry_price=0.92, size_usdc=50.0,
                p_signal=0.95, won=True,
            )
        rwr = bot_stats.rolling_win_rate(50)
        assert rwr == 1.0

    def test_empty_stats(self, bot_stats):
        assert bot_stats.total_trades == 0
        assert bot_stats.win_rate is None
        assert bot_stats.rolling_win_rate() is None


class TestToDict:
    def test_export_keys(self, bot_stats):
        d = bot_stats.to_dict()
        assert "wins" in d
        assert "losses" in d
        assert "total_pnl" in d
        assert "win_rate" in d
        assert "rolling_win_rate" in d
