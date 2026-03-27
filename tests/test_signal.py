"""Tests for signal generation logic."""
import math
import time

import pytest

from market_calculator import MarketCalculator, K_SIGNAL_DEFAULT, _phi_inv
from ws_orderbook import OrderBookWS, OrderBook, Candle5m, Candle1m


class FakeOrderBookWS:
    """Minimal mock of OrderBookWS for signal tests."""

    def __init__(self):
        self.book = OrderBook()
        self.candle = Candle5m()
        self.candle_1m = Candle1m()
        self._candle_returns = []

    @property
    def realized_vol_5m(self):
        if len(self._candle_returns) < 3:
            return 0.0022
        returns = self._candle_returns
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(var) if var > 0 else 0.0022


class TestLogisticSignal:
    """Tests for the core logistic return signal."""

    def _make_calc(self, mid_price, open_price=100_000.0):
        ws = FakeOrderBookWS()
        ws.book.bids = [(mid_price - 0.5, 1.0)]
        ws.book.asks = [(mid_price + 0.5, 1.0)]
        ws.book.last_update_ms = int(time.time() * 1000)
        ws.book._bid_volume = 1.0
        ws.book._ask_volume = 1.0
        ws.candle = Candle5m(open=open_price, close=mid_price)
        calc = MarketCalculator(ws)
        return calc

    def test_zero_return_gives_half(self, sample_market):
        """When BTC hasn't moved, signal should be ~0.5."""
        calc = self._make_calc(100_000.0, 100_000.0)
        p = calc.p_up_signal(sample_market)
        assert abs(p - 0.5) < 0.02  # allow small OFI noise

    def test_positive_return_gives_above_half(self, sample_market):
        """Positive BTC return → p_up > 0.5."""
        calc = self._make_calc(100_100.0, 100_000.0)
        p = calc.p_up_signal(sample_market)
        assert p > 0.5

    def test_negative_return_gives_below_half(self, sample_market):
        """Negative BTC return → p_up < 0.5."""
        calc = self._make_calc(99_900.0, 100_000.0)
        p = calc.p_up_signal(sample_market)
        assert p < 0.5

    def test_large_positive_return_near_one(self, sample_market):
        """Large positive return should saturate near 1.0."""
        calc = self._make_calc(100_500.0, 100_000.0)  # +0.5%
        p = calc.p_up_signal(sample_market)
        assert p > 0.95

    def test_large_negative_return_near_zero(self, sample_market):
        """Large negative return should saturate near 0.0."""
        calc = self._make_calc(99_500.0, 100_000.0)  # -0.5%
        p = calc.p_up_signal(sample_market)
        assert p < 0.05

    def test_none_mid_price_returns_half(self, sample_market):
        """If mid_price is None, return neutral 0.5."""
        ws = FakeOrderBookWS()
        # Empty book → mid_price is None
        calc = MarketCalculator(ws)
        p = calc.p_up_signal(sample_market)
        assert p == 0.5

    def test_stale_data_returns_half(self, sample_market):
        """If order book data is stale, return neutral 0.5."""
        ws = FakeOrderBookWS()
        ws.book.bids = [(100_100.0, 1.0)]
        ws.book.asks = [(100_101.0, 1.0)]
        ws.book.last_update_ms = int(time.time() * 1000) - 5000  # 5s old
        ws.book._bid_volume = 1.0
        ws.book._ask_volume = 1.0
        calc = MarketCalculator(ws)
        p = calc.p_up_signal(sample_market)
        assert p == 0.5

    def test_signal_is_bounded(self, sample_market):
        """Signal should always be in [0.01, 0.99]."""
        for mid in [50_000, 99_000, 100_000, 101_000, 150_000]:
            calc = self._make_calc(float(mid), 100_000.0)
            p = calc.p_up_signal(sample_market)
            assert 0.01 <= p <= 0.99


class TestAdaptiveK:
    """Tests for adaptive K calibration."""

    def test_default_vol_gives_reasonable_k(self):
        ws = FakeOrderBookWS()
        calc = MarketCalculator(ws)
        k = calc.adaptive_k()
        # With default σ=0.22%, k should be around 4000-5000
        assert 500 <= k <= 8000

    def test_low_vol_gives_higher_k(self):
        """Lower volatility → larger K (signal more responsive)."""
        ws_low = FakeOrderBookWS()
        ws_low._candle_returns = [0.0001] * 10  # very low vol
        ws_high = FakeOrderBookWS()
        ws_high._candle_returns = [0.005, -0.004, 0.003, -0.006, 0.004,
                                    -0.003, 0.005, -0.004, 0.003, -0.005]
        calc_low = MarketCalculator(ws_low)
        calc_high = MarketCalculator(ws_high)
        assert calc_low.adaptive_k() > calc_high.adaptive_k()


class TestOrderFlowImbalance:
    """Tests for order book imbalance signal."""

    def test_balanced_book_gives_zero(self, sample_orderbook):
        """Equal bid/ask volume → imbalance = 0."""
        sample_orderbook._bid_volume = 5.0
        sample_orderbook._ask_volume = 5.0
        assert sample_orderbook.order_flow_imbalance == 0.0

    def test_bid_heavy_gives_positive(self, sample_orderbook):
        """More bid volume → positive imbalance (buy pressure)."""
        sample_orderbook._bid_volume = 8.0
        sample_orderbook._ask_volume = 2.0
        assert sample_orderbook.order_flow_imbalance > 0

    def test_ask_heavy_gives_negative(self, sample_orderbook):
        """More ask volume → negative imbalance (sell pressure)."""
        sample_orderbook._bid_volume = 2.0
        sample_orderbook._ask_volume = 8.0
        assert sample_orderbook.order_flow_imbalance < 0

    def test_empty_book_gives_zero(self):
        """Empty book → imbalance = 0."""
        book = OrderBook()
        assert book.order_flow_imbalance == 0.0


class TestPhiInverse:
    """Tests for the probit function approximation."""

    def test_half_gives_zero(self):
        assert abs(_phi_inv(0.5)) < 0.001

    def test_high_p_gives_positive(self):
        assert _phi_inv(0.95) > 1.5

    def test_low_p_gives_negative(self):
        assert _phi_inv(0.05) < -1.5

    def test_symmetry(self):
        assert abs(_phi_inv(0.1) + _phi_inv(0.9)) < 0.01
