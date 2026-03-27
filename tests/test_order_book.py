"""Tests for OrderBook and candle data structures."""
import time

import pytest

from ws_orderbook import OrderBook, Candle5m, Candle1m, OrderBookWS


class TestOrderBook:
    def test_mid_price(self, sample_orderbook):
        mid = sample_orderbook.mid_price
        assert mid == (100_000.0 + 100_001.0) / 2

    def test_best_bid_ask(self, sample_orderbook):
        assert sample_orderbook.best_bid == 100_000.0
        assert sample_orderbook.best_ask == 100_001.0

    def test_spread_bps(self, sample_orderbook):
        spread = sample_orderbook.spread_bps
        expected = (100_001.0 - 100_000.0) / 100_000.0 * 10_000
        assert abs(spread - expected) < 0.01

    def test_empty_book(self):
        book = OrderBook()
        assert book.mid_price is None
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.spread_bps is None

    def test_bid_ask_volume(self, sample_orderbook):
        assert sample_orderbook.bid_volume > 0
        assert sample_orderbook.ask_volume > 0

    def test_staleness_fresh(self, sample_orderbook):
        assert not sample_orderbook.is_stale

    def test_staleness_old(self):
        book = OrderBook()
        book.bids = [(100.0, 1.0)]
        book.asks = [(101.0, 1.0)]
        book.last_update_ms = int(time.time() * 1000) - 5000
        assert book.is_stale

    def test_staleness_never_updated(self):
        book = OrderBook()
        assert book.is_stale


class TestCandle5m:
    def test_volatility_bps(self, sample_candle):
        vol = sample_candle.volatility_bps
        expected = (100_050.0 - 99_960.0) / 100_030.0 * 10_000
        assert abs(vol - expected) < 0.1

    def test_return_pct(self, sample_candle):
        ret = sample_candle.return_pct
        expected = (100_030.0 - 100_000.0) / 100_000.0
        assert abs(ret - expected) < 1e-8

    def test_zero_close_vol(self):
        c = Candle5m(open=100, high=101, low=99, close=0)
        assert c.volatility_bps == 0.0

    def test_zero_open_return(self):
        c = Candle5m(open=0, close=100)
        assert c.return_pct == 0.0


class TestCandle1m:
    def test_bullish_direction(self):
        c = Candle1m(open=100.0, close=101.0)
        assert c.direction == 1

    def test_bearish_direction(self):
        c = Candle1m(open=101.0, close=100.0)
        assert c.direction == -1

    def test_flat_direction(self):
        c = Candle1m(open=100.0, close=100.0)
        assert c.direction == 0


class TestOrderBookWSRealizedVol:
    def test_default_vol_with_insufficient_data(self):
        ws = OrderBookWS()
        assert ws.realized_vol_5m == 0.0022

    def test_vol_with_enough_data(self):
        ws = OrderBookWS()
        # Add some returns
        for r in [0.001, -0.001, 0.002, -0.002, 0.001]:
            ws._candle_returns.append(r)
        vol = ws.realized_vol_5m
        assert vol > 0
        assert vol != 0.0022  # should differ from default
