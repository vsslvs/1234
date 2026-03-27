"""
Binance WebSocket order book client.

Subscribes to:
  - <symbol>@depth20@100ms  →  live 20-level order book (every 100ms)
  - <symbol>@kline_5m       →  5-minute candles for volatility estimation
  - <symbol>@kline_1m       →  1-minute candles for multi-timeframe confirmation
"""
import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

import websockets
from websockets.exceptions import ConnectionClosed

from config import Config

log = logging.getLogger(__name__)

Price = float
Qty = float

# Number of closed 5m candles to keep for realized volatility
_VOL_HISTORY_SIZE = 24  # 2 hours of 5m candles


@dataclass
class OrderBook:
    """Live best bid/ask and top N levels with volume aggregation."""
    bids: List[Tuple[Price, Qty]] = field(default_factory=list)  # [(price, qty), ...]
    asks: List[Tuple[Price, Qty]] = field(default_factory=list)
    last_update_ms: int = 0

    # Aggregated volumes (top 5 levels) for order-flow imbalance signal
    _bid_volume: float = 0.0
    _ask_volume: float = 0.0

    @property
    def best_bid(self) -> Optional[Price]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[Price]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid_price(self) -> Optional[Price]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread_bps(self) -> Optional[float]:
        if self.best_bid and self.best_ask and self.best_bid > 0:
            return (self.best_ask - self.best_bid) / self.best_bid * 10_000
        return None

    @property
    def bid_volume(self) -> float:
        return self._bid_volume

    @property
    def ask_volume(self) -> float:
        return self._ask_volume

    @property
    def order_flow_imbalance(self) -> float:
        """
        Order flow imbalance from top-of-book volumes.
        Returns [-1, +1]: positive = buy pressure, negative = sell pressure.
        """
        total = self._bid_volume + self._ask_volume
        if total == 0:
            return 0.0
        return (self._bid_volume - self._ask_volume) / total

    @property
    def is_stale(self) -> bool:
        """True if last update is older than STALENESS_THRESHOLD_MS."""
        if self.last_update_ms == 0:
            return True
        age_ms = int(time.time() * 1000) - self.last_update_ms
        return age_ms > Config.STALENESS_THRESHOLD_MS


@dataclass
class Candle5m:
    """Most recent 5-minute candle (may be open or closed)."""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    is_closed: bool = False

    @property
    def volatility_bps(self) -> float:
        """High-low range as basis points relative to close price."""
        if self.close == 0:
            return 0.0
        return (self.high - self.low) / self.close * 10_000

    @property
    def return_pct(self) -> float:
        """Candle return as a fraction (close/open - 1)."""
        if self.open == 0:
            return 0.0
        return (self.close - self.open) / self.open


@dataclass
class Candle1m:
    """Most recent 1-minute candle for multi-timeframe confirmation."""
    open: float = 0.0
    close: float = 0.0
    is_closed: bool = False

    @property
    def direction(self) -> int:
        """Returns +1 if bullish, -1 if bearish, 0 if flat."""
        if self.close > self.open:
            return 1
        elif self.close < self.open:
            return -1
        return 0


class OrderBookWS:
    """
    Manages Binance WebSocket streams:
      - <symbol>@depth20@100ms  →  live order book
      - <symbol>@kline_5m       →  5m candles for volatility
      - <symbol>@kline_1m       →  1m candles for confirmation

    Usage:
        ob = OrderBookWS()
        asyncio.create_task(ob.run())
        # then read ob.book, ob.candle, ob.candle_1m freely
    """

    def __init__(self):
        self.book = OrderBook()
        self.candle = Candle5m()
        self.candle_1m = Candle1m()
        self._running = False
        self._reconnect_delay = 1.0  # seconds, doubles on each failure

        # Rolling history of closed 5m candle returns for realized vol
        self._candle_returns: Deque[float] = deque(maxlen=_VOL_HISTORY_SIZE)

    @property
    def realized_vol_5m(self) -> float:
        """
        Realized 5-minute return volatility (standard deviation of returns).
        Returns 0.0022 (default) if insufficient history.
        """
        if len(self._candle_returns) < 3:
            return 0.0022  # default σ₅ = 0.22%
        returns = list(self._candle_returns)
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(variance) if variance > 0 else 0.0022

    def _stream_url(self) -> str:
        symbol = Config.BTC_SYMBOL.lower()
        streams = (
            f"{symbol}@depth20@100ms/"
            f"{symbol}@kline_5m/"
            f"{symbol}@kline_1m"
        )
        # Combined streams require /stream?streams= endpoint (not /ws/<path>).
        base = Config.BINANCE_WS_URL.removesuffix("/ws")
        return f"{base}/stream?streams={streams}"

    async def run(self) -> None:
        """Connect and keep reconnecting on failure."""
        self._running = True
        while self._running:
            try:
                await self._connect()
                self._reconnect_delay = 1.0  # reset on clean connect
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                log.warning("WS disconnected: %s – reconnecting in %.1fs", exc, self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)
            except asyncio.CancelledError:
                break

    async def _connect(self) -> None:
        url = self._stream_url()
        log.info("Connecting to %s", url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            log.info("Order book WS connected")
            async for raw in ws:
                self._handle(raw)

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        stream = msg.get("stream", "")
        data = msg.get("data", msg)  # combined streams wrap in {"stream":..,"data":..}

        if "depth" in stream:
            self._update_book(data)
        elif "kline_5m" in stream:
            self._update_candle(data)
        elif "kline_1m" in stream:
            self._update_candle_1m(data)

    def _update_book(self, data: dict) -> None:
        self.book.bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        self.book.asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
        self.book.last_update_ms = int(time.time() * 1000)

        # Aggregate top-5 volumes for order-flow imbalance
        self.book._bid_volume = sum(q for _, q in self.book.bids[:5])
        self.book._ask_volume = sum(q for _, q in self.book.asks[:5])

    def _update_candle(self, data: dict) -> None:
        k = data.get("k", {})
        new_candle = Candle5m(
            open=float(k.get("o", 0)),
            high=float(k.get("h", 0)),
            low=float(k.get("l", 0)),
            close=float(k.get("c", 0)),
            volume=float(k.get("v", 0)),
            is_closed=bool(k.get("x", False)),
        )

        # Track closed candle returns for realized vol
        if new_candle.is_closed and not self.candle.is_closed:
            ret = new_candle.return_pct
            self._candle_returns.append(ret)

        self.candle = new_candle

    def _update_candle_1m(self, data: dict) -> None:
        k = data.get("k", {})
        self.candle_1m = Candle1m(
            open=float(k.get("o", 0)),
            close=float(k.get("c", 0)),
            is_closed=bool(k.get("x", False)),
        )

    def stop(self) -> None:
        self._running = False
