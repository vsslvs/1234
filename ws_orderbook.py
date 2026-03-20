"""
Binance WebSocket order book client.

Subscribes to <symbol>@depth20@100ms for a full 20-level depth snapshot
refreshed every 100 ms, and <symbol>@kline_5m for 5-minute candle data
used in volatility estimation.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import websockets
from websockets.exceptions import ConnectionClosed

from config import Config

log = logging.getLogger(__name__)

Price = float
Qty = float


@dataclass
class OrderBook:
    """Live best bid/ask and top N levels."""
    bids: List[Tuple[Price, Qty]] = field(default_factory=list)  # [(price, qty), ...]
    asks: List[Tuple[Price, Qty]] = field(default_factory=list)
    last_update_ms: int = 0

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


@dataclass
class Candle5m:
    """Most recent closed 5-minute candle."""
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


class OrderBookWS:
    """
    Manages two Binance WebSocket streams:
      - <symbol>@depth20@100ms  →  live order book
      - <symbol>@kline_5m       →  5m candles for volatility

    Usage:
        ob = OrderBookWS()
        asyncio.create_task(ob.run())
        # then read ob.book and ob.candle freely
    """

    def __init__(self):
        self.book = OrderBook()
        self.candle = Candle5m()
        self._running = False
        self._reconnect_delay = 1.0  # seconds, doubles on each failure

    def _stream_url(self) -> str:
        symbol = Config.BTC_SYMBOL.lower()
        streams = f"{symbol}@depth20@100ms/{symbol}@kline_5m"
        return f"{Config.BINANCE_WS_URL}/{streams}"

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
        elif "kline" in stream:
            self._update_candle(data)

    def _update_book(self, data: dict) -> None:
        self.book.bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        self.book.asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
        self.book.last_update_ms = int(time.time() * 1000)

    def _update_candle(self, data: dict) -> None:
        k = data.get("k", {})
        self.candle = Candle5m(
            open=float(k.get("o", 0)),
            high=float(k.get("h", 0)),
            low=float(k.get("l", 0)),
            close=float(k.get("c", 0)),
            volume=float(k.get("v", 0)),
            is_closed=bool(k.get("x", False)),
        )

    def stop(self) -> None:
        self._running = False
