"""Integration tests for MarketMaker window lifecycle."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_maker import MarketMaker, WindowState, MarketSide, P_UP_THRESHOLD
from market_calculator import BtcMarket
from config import Config


class FakeOrderBookWS:
    """Minimal mock of OrderBookWS."""

    def __init__(self, mid=100_100.0, open_price=100_000.0):
        from ws_orderbook import OrderBook, Candle5m, Candle1m
        self.book = OrderBook()
        self.book.bids = [(mid - 0.5, 1.0)]
        self.book.asks = [(mid + 0.5, 1.0)]
        self.book.last_update_ms = int(time.time() * 1000)
        self.book._bid_volume = 1.0
        self.book._ask_volume = 1.0
        self.candle = Candle5m(
            open=open_price, high=mid + 10, low=open_price - 10,
            close=mid, volume=100.0, is_closed=False,
        )
        self.candle_1m = Candle1m(open=mid - 5, close=mid)
        self._candle_returns = []

    @property
    def realized_vol_5m(self):
        return 0.0022


def _make_market(seconds_to_close=8.0, open_price=100_000.0):
    """Create a BtcMarket with controlled timing."""
    now = time.time()
    end = now + seconds_to_close
    start = end - 300
    return BtcMarket(
        question_id="q1",
        condition_id="c1",
        yes_token_id="yes-tok",
        no_token_id="no-tok",
        window_start=int(start),
        window_end=int(end),
        open_price=open_price,
    )


class TestWindowState:
    def test_new_state_no_orders(self):
        market = _make_market()
        state = WindowState(market)
        assert not state.yes.has_order
        assert not state.no.has_order
        assert state.all_orders() == []

    def test_side_labels(self):
        market = _make_market()
        state = WindowState(market)
        assert state.yes.side_label == "YES"
        assert state.no.side_label == "NO"


class TestMarketSide:
    def test_price_drift_no_order(self):
        side = MarketSide("tok", "YES")
        assert not side.price_drifted(0.92)

    def test_price_drift_within_threshold(self):
        from polymarket_client import MakerOrder
        side = MarketSide("tok", "YES")
        side.order = MakerOrder(
            order_id="o1", token_id="tok", side=0,
            price=0.92, size_usdc=50, fee_rate_bps=0,
            placed_at=time.monotonic(),
        )
        assert not side.price_drifted(0.922)  # 0.002 < threshold 0.005

    def test_price_drift_beyond_threshold(self):
        from polymarket_client import MakerOrder
        side = MarketSide("tok", "YES")
        side.order = MakerOrder(
            order_id="o1", token_id="tok", side=0,
            price=0.92, size_usdc=50, fee_rate_bps=0,
            placed_at=time.monotonic(),
        )
        assert side.price_drifted(0.93)  # 0.01 > threshold 0.005


class TestEvaluateWindow:
    def test_win_yes_when_btc_up(self):
        """YES side should win when BTC closes up."""
        ob_ws = FakeOrderBookWS(mid=100_100.0, open_price=100_000.0)
        client = AsyncMock()
        from market_calculator import MarketCalculator
        calc = MarketCalculator(ob_ws)

        mm = MarketMaker(client, calc, ob_ws)
        market = _make_market(open_price=100_000.0)
        state = WindowState(market)

        # Simulate YES side was active
        state.yes.was_ever_active = True
        state.yes.p_signal_at_entry = 0.95
        state.yes.last_entry_price = 0.92
        state.yes.last_entry_size = 50.0

        mm._evaluate_and_record_window(state)
        assert mm._stats.total_trades == 1
        assert mm._stats._wins == 1

    def test_loss_yes_when_btc_down(self):
        """YES side should lose when BTC closes down."""
        ob_ws = FakeOrderBookWS(mid=99_900.0, open_price=100_000.0)
        client = AsyncMock()
        from market_calculator import MarketCalculator
        calc = MarketCalculator(ob_ws)

        mm = MarketMaker(client, calc, ob_ws)
        market = _make_market(open_price=100_000.0)
        state = WindowState(market)

        state.yes.was_ever_active = True
        state.yes.p_signal_at_entry = 0.95
        state.yes.last_entry_price = 0.92
        state.yes.last_entry_size = 50.0

        mm._evaluate_and_record_window(state)
        assert mm._stats._losses == 1

    def test_inactive_side_not_recorded(self):
        """Sides that were never active should not generate trades."""
        ob_ws = FakeOrderBookWS()
        client = AsyncMock()
        from market_calculator import MarketCalculator
        calc = MarketCalculator(ob_ws)

        mm = MarketMaker(client, calc, ob_ws)
        market = _make_market()
        state = WindowState(market)
        # Both sides inactive
        mm._evaluate_and_record_window(state)
        assert mm._stats.total_trades == 0

    def test_risk_manager_updated_on_resolution(self):
        """Risk manager should receive resolution events."""
        ob_ws = FakeOrderBookWS(mid=100_100.0, open_price=100_000.0)
        client = AsyncMock()
        from market_calculator import MarketCalculator
        calc = MarketCalculator(ob_ws)

        mm = MarketMaker(client, calc, ob_ws)
        market = _make_market(open_price=100_000.0)
        state = WindowState(market)

        state.yes.was_ever_active = True
        state.yes.p_signal_at_entry = 0.95
        state.yes.last_entry_price = 0.92
        state.yes.last_entry_size = 50.0

        mm._evaluate_and_record_window(state)
        assert mm._risk.session_pnl() != 0.0
