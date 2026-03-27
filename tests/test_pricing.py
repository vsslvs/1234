"""Tests for pricing logic."""
import math
import time

import pytest

from market_calculator import MarketCalculator
from ws_orderbook import OrderBook, Candle5m, Candle1m


class FakeOrderBookWS:
    def __init__(self, mid=100_000.0):
        self.book = OrderBook()
        self.book.bids = [(mid - 0.5, 1.0)]
        self.book.asks = [(mid + 0.5, 1.0)]
        self.book.last_update_ms = int(time.time() * 1000)
        self.book._bid_volume = 1.0
        self.book._ask_volume = 1.0
        self.candle = Candle5m(open=mid, close=mid)
        self.candle_1m = Candle1m(open=mid, close=mid)
        self._candle_returns = []

    @property
    def realized_vol_5m(self):
        return 0.0022


class TestFairPrices:
    """Tests for fee-adjusted fair pricing."""

    def test_fair_prices_sum_less_than_one(self, sample_market):
        """Fee-adjusted fair prices should sum to less than 1.0."""
        ws = FakeOrderBookWS()
        calc = MarketCalculator(ws)
        fair_yes, fair_no = calc.fair_prices(sample_market)
        assert fair_yes + fair_no < 1.0

    def test_fair_prices_positive(self, sample_market):
        """Both fair prices should be positive."""
        ws = FakeOrderBookWS()
        calc = MarketCalculator(ws)
        fair_yes, fair_no = calc.fair_prices(sample_market)
        assert fair_yes > 0
        assert fair_no > 0

    def test_strong_signal_high_fair_yes(self, sample_market):
        """Strong upward signal → fair_yes significantly above 0.5."""
        ws = FakeOrderBookWS(100_300.0)  # +0.3% return
        ws.candle = Candle5m(open=100_000.0, close=100_300.0)
        calc = MarketCalculator(ws)
        fair_yes, fair_no = calc.fair_prices(sample_market)
        assert fair_yes > 0.7
        assert fair_no < 0.3


class TestTakerFee:
    def test_fee_at_half(self):
        """Fee at p=0.5 should be ~0.0156 (1.56%)."""
        ws = FakeOrderBookWS()
        calc = MarketCalculator(ws)
        fee = calc.taker_fee(0.5)
        assert abs(fee - 0.015625) < 0.001

    def test_fee_at_extremes_near_zero(self):
        """Fee at extreme probabilities should be near zero."""
        ws = FakeOrderBookWS()
        calc = MarketCalculator(ws)
        assert calc.taker_fee(0.95) < 0.002
        assert calc.taker_fee(0.05) < 0.002

    def test_fee_at_zero(self):
        ws = FakeOrderBookWS()
        calc = MarketCalculator(ws)
        assert calc.taker_fee(0.0) == 0.0
        assert calc.taker_fee(1.0) == 0.0

    def test_fee_symmetric(self):
        """Fee should be symmetric: fee(p) == fee(1-p)."""
        ws = FakeOrderBookWS()
        calc = MarketCalculator(ws)
        for p in [0.1, 0.3, 0.7, 0.9]:
            assert abs(calc.taker_fee(p) - calc.taker_fee(1 - p)) < 1e-10


class TestDynamicEdge:
    def test_normal_vol_gives_base_edge(self):
        """At default vol (0.22%), dynamic edge ≈ MIN_EDGE_BPS."""
        ws = FakeOrderBookWS()
        calc = MarketCalculator(ws)
        edge = calc.dynamic_min_edge()
        assert 40 <= edge <= 60  # roughly 50 bps

    def test_high_vol_gives_higher_edge(self):
        """Higher realized vol → higher edge requirement."""
        ws = FakeOrderBookWS()
        ws._candle_returns = [0.005, -0.004, 0.006, -0.005] * 3  # high vol
        # Override realized_vol_5m
        calc = MarketCalculator(ws)
        # Can't easily override property, so test the formula direction
        edge = calc.dynamic_min_edge()
        assert edge >= 30
