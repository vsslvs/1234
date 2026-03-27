"""Shared test fixtures for the Polymarket BTC market maker bot."""
import os
import time

import pytest

# Set required env vars BEFORE importing any project modules
os.environ.setdefault("POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("PAPER_MODE", "true")


from ws_orderbook import OrderBook, Candle5m, Candle1m, OrderBookWS
from market_calculator import BtcMarket
from risk_manager import RiskManager
from stats import BotStats


@pytest.fixture
def sample_orderbook() -> OrderBook:
    """OrderBook with realistic BTC prices."""
    book = OrderBook()
    book.bids = [(100_000.0, 1.5), (99_999.0, 2.0), (99_998.0, 3.0),
                 (99_997.0, 1.0), (99_996.0, 0.5)]
    book.asks = [(100_001.0, 1.0), (100_002.0, 2.5), (100_003.0, 1.5),
                 (100_004.0, 0.8), (100_005.0, 0.3)]
    book.last_update_ms = int(time.time() * 1000)
    book._bid_volume = sum(q for _, q in book.bids[:5])
    book._ask_volume = sum(q for _, q in book.asks[:5])
    return book


@pytest.fixture
def sample_candle() -> Candle5m:
    """Typical 5m candle with moderate volatility."""
    return Candle5m(
        open=100_000.0,
        high=100_050.0,
        low=99_960.0,
        close=100_030.0,
        volume=150.0,
        is_closed=True,
    )


@pytest.fixture
def sample_candle_1m() -> Candle1m:
    """Bullish 1m candle."""
    return Candle1m(open=100_000.0, close=100_020.0, is_closed=True)


@pytest.fixture
def sample_market() -> BtcMarket:
    """BtcMarket for testing with window starting now."""
    now = int(time.time())
    ws = (now // 300) * 300
    return BtcMarket(
        question_id="test-q",
        condition_id="test-c",
        yes_token_id="token-yes-123",
        no_token_id="token-no-456",
        window_start=ws,
        window_end=ws + 300,
        open_price=100_000.0,
    )


@pytest.fixture
def risk_manager() -> RiskManager:
    """Fresh RiskManager with default config."""
    return RiskManager()


@pytest.fixture
def bot_stats() -> BotStats:
    """Fresh BotStats instance."""
    return BotStats()
