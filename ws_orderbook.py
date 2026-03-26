"""
Binance WebSocket order book client with advanced market microstructure.

Subscribes to three combined streams:
  - <symbol>@depth20@100ms   →  live 20-level order book (refreshed every 100 ms)
  - <symbol>@kline_5m        →  5-minute candles for volatility + window open price
  - <symbol>@kline_1h        →  1-hour candles for higher-timeframe trend bias

Market microstructure features
------------------------------
- VWAP tracking: volume-weighted average price for more robust return estimation
- Multi-level OBI: weighted imbalance across all 20 orderbook levels (not just top 5)
- Tick direction tracking: tracks uptick/downtick sequences for momentum detection
- Trade flow imbalance: approximates buy/sell aggressor ratio from tick direction

Adaptive volatility
-------------------
Tracks returns of the last N closed 5-minute candles to compute realized σ.
Uses exponential weighting with configurable half-life so recent candles
contribute more. Bessel-corrected for unbiased variance estimation.

Volatility regime detection
----------------------------
Classifies current vol into 'calm', 'normal', or 'storm' based on percentile
rank vs recent history. Storm mode widens spreads and reduces size.
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


@dataclass
class OrderBook:
    """Live best bid/ask and top N levels with microstructure metrics."""
    bids: List[Tuple[Price, Qty]] = field(default_factory=list)
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
    def micro_price(self) -> Optional[Price]:
        """
        Micro-price: volume-weighted mid price.
        More accurate fair value estimate than simple mid.
        micro_price = (ask_qty × bid + bid_qty × ask) / (bid_qty + ask_qty)
        When ask_qty >> bid_qty, micro_price is pulled toward the ask (upward pressure).
        """
        if not self.bids or not self.asks:
            return self.mid_price
        bid_p, bid_q = self.bids[0]
        ask_p, ask_q = self.asks[0]
        total_q = bid_q + ask_q
        if total_q < 1e-10:
            return self.mid_price
        return (ask_q * bid_p + bid_q * ask_p) / total_q

    @property
    def spread_bps(self) -> Optional[float]:
        if self.best_bid and self.best_ask and self.best_bid > 0:
            return (self.best_ask - self.best_bid) / self.best_bid * 10_000
        return None

    @property
    def imbalance(self) -> float:
        """
        Order book imbalance from top 5 levels: (bid_vol - ask_vol) / total.
        Returns [-1, +1]. Positive = more buyers, negative = more sellers.
        """
        bid_vol = sum(qty for _, qty in self.bids[:5]) if len(self.bids) >= 5 else 0.0
        ask_vol = sum(qty for _, qty in self.asks[:5]) if len(self.asks) >= 5 else 0.0
        total = bid_vol + ask_vol
        if total < 1e-10:
            return 0.0
        return (bid_vol - ask_vol) / total

    @property
    def weighted_imbalance(self) -> float:
        """
        Depth-weighted OBI across all available levels.
        Closer levels get exponentially higher weight (decay factor = 0.7).
        More informative than flat top-5 imbalance for detecting real pressure.
        """
        if not self.bids or not self.asks:
            return 0.0
        decay = 0.7
        bid_weighted = 0.0
        ask_weighted = 0.0
        n_levels = min(len(self.bids), len(self.asks), 20)
        for i in range(n_levels):
            w = decay ** i
            bid_weighted += w * self.bids[i][1]
            ask_weighted += w * self.asks[i][1]
        total = bid_weighted + ask_weighted
        if total < 1e-10:
            return 0.0
        return (bid_weighted - ask_weighted) / total

    @property
    def depth_ratio(self) -> float:
        """
        Total bid depth / total ask depth across all levels.
        > 1.0 = buy wall dominance, < 1.0 = sell wall dominance.
        """
        if not self.bids or not self.asks:
            return 1.0
        bid_total = sum(q for _, q in self.bids)
        ask_total = sum(q for _, q in self.asks)
        if ask_total < 1e-10:
            return 1.0
        return bid_total / ask_total


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
    def close_location(self) -> float:
        """
        Where the candle closed within its high-low range.
        1.0 = closed at high (bullish), 0.0 = closed at low (bearish).
        """
        hl_range = self.high - self.low
        if hl_range < 1e-10:
            return 0.5
        return (self.close - self.low) / hl_range

    @property
    def body_ratio(self) -> float:
        """
        Candle body size / range. High = strong directional candle, low = doji/indecision.
        """
        hl_range = self.high - self.low
        if hl_range < 1e-10:
            return 0.0
        return abs(self.close - self.open) / hl_range

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open


@dataclass
class Candle1h:
    """Current 1-hour candle (may still be open)."""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    is_closed: bool = False

    @property
    def ret(self) -> float:
        """Hourly return: (close - open) / open.  Positive = uptrend."""
        if self.open <= 0:
            return 0.0
        return (self.close - self.open) / self.open


class OrderBookWS:
    """
    Manages three Binance WebSocket streams:
      - <symbol>@depth20@100ms  →  live order book
      - <symbol>@kline_5m       →  5m candles for volatility
      - <symbol>@kline_1h       →  1h candles for trend bias

    Exposes derived microstructure properties:
      - realized_sigma_5m: adaptive volatility from recent candles
      - smoothed_obi: EMA of order book imbalance
      - weighted_obi: depth-weighted OBI (more informative)
      - volume_ratio: current vs median candle volume
      - tick_momentum: recent uptick/downtick ratio
      - vwap_5m: VWAP of current 5-minute period
    """

    _MIN_CANDLES_FOR_SIGMA = 6
    _SIGMA_HALF_LIFE = 12.0  # candle units (~1 hour)

    def __init__(self):
        self.book = OrderBook()
        self.candle = Candle5m()
        self.candle_1h = Candle1h()
        self._running = False
        self._reconnect_delay = 1.0
        self._last_disconnect: float = 0.0

        # Realized vol: returns of last 48 closed 5m candles (~4 hours)
        self._closed_returns: Deque[float] = deque(maxlen=48)
        self._closed_volumes: Deque[float] = deque(maxlen=48)

        # OBI smoothing: EMA of recent imbalance values
        self._obi_history: Deque[float] = deque(maxlen=50)
        # Weighted OBI history (separate from simple OBI)
        self._wobi_history: Deque[float] = deque(maxlen=50)

        # Last fully closed 5m candle
        self._last_closed_candle: Optional[Candle5m] = None

        # Tick direction tracking
        self._last_mid: Optional[float] = None
        self._tick_directions: Deque[int] = deque(maxlen=100)  # +1 uptick, -1 downtick

        # VWAP tracking within current 5m window
        self._vwap_price_volume: float = 0.0  # Σ(price × volume)
        self._vwap_volume: float = 0.0         # Σ(volume)

        # Consecutive candle direction tracking
        self._candle_directions: Deque[int] = deque(maxlen=12)  # +1 bullish, -1 bearish

    def _stream_url(self) -> str:
        symbol = Config.BTC_SYMBOL.lower()
        streams = f"{symbol}@depth20@100ms/{symbol}@kline_5m/{symbol}@kline_1h"
        base = Config.BINANCE_WS_URL.removesuffix("/ws")
        return f"{base}/stream?streams={streams}"

    async def run(self) -> None:
        """Connect and keep reconnecting on failure."""
        self._running = True
        while self._running:
            try:
                await self._connect()
                self._reconnect_delay = 1.0
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                now = time.monotonic()
                if now - self._last_disconnect > 60.0:
                    self._reconnect_delay = 1.0
                self._last_disconnect = now
                log.warning("WS disconnected: %s \u2013 reconnecting in %.1fs", exc, self._reconnect_delay)
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
        data = msg.get("data", msg)

        if "depth" in stream:
            self._update_book(data)
        elif "kline_1h" in stream:
            self._update_candle_1h(data)
        elif "kline" in stream:
            self._update_candle(data)

    def _update_book(self, data: dict) -> None:
        self.book.bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        self.book.asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
        self.book.last_update_ms = int(time.time() * 1000)

        # Track OBI for smoothed signal
        self._obi_history.append(self.book.imbalance)
        self._wobi_history.append(self.book.weighted_imbalance)

        # Track tick direction for momentum
        mid = self.book.mid_price
        if mid is not None and self._last_mid is not None:
            diff = mid - self._last_mid
            # Threshold: 0.01% of price (≈$8.7 at $87k BTC) — filters out
            # sub-cent noise while catching real orderbook-level moves.
            threshold = self._last_mid * 0.0001
            if abs(diff) > threshold:
                self._tick_directions.append(1 if diff > 0 else -1)
        self._last_mid = mid

        # VWAP accumulation: weight by top-of-book volume (actual liquidity)
        if mid is not None:
            # Use total top-5 bid+ask volume as weight — real proxy for activity
            bid_vol = sum(q for _, q in self.book.bids[:5]) if self.book.bids else 0.0
            ask_vol = sum(q for _, q in self.book.asks[:5]) if self.book.asks else 0.0
            weight = max(0.001, bid_vol + ask_vol)
            self._vwap_price_volume += mid * weight
            self._vwap_volume += weight

    def _update_candle(self, data: dict) -> None:
        k = data.get("k", {})
        is_closed = bool(k.get("x", False))
        o = float(k.get("o", 0))
        c = float(k.get("c", 0))

        self.candle = Candle5m(
            open=o,
            high=float(k.get("h", 0)),
            low=float(k.get("l", 0)),
            close=c,
            volume=float(k.get("v", 0)),
            is_closed=is_closed,
        )

        if is_closed and o > 0:
            self._closed_returns.append((c - o) / o)
            self._closed_volumes.append(float(k.get("v", 0)))
            self._last_closed_candle = self.candle
            self._candle_directions.append(1 if c > o else -1)

            # Reset VWAP for next window
            self._vwap_price_volume = 0.0
            self._vwap_volume = 0.0

    def _update_candle_1h(self, data: dict) -> None:
        k = data.get("k", {})
        self.candle_1h = Candle1h(
            open=float(k.get("o", 0)),
            high=float(k.get("h", 0)),
            low=float(k.get("l", 0)),
            close=float(k.get("c", 0)),
            is_closed=bool(k.get("x", False)),
        )

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def hourly_trend_bias(self) -> float:
        """
        Trend bias from 1h candle return, clamped to [-1, +1].
        Positive = bullish (favour YES), negative = bearish (favour NO).
        """
        ret = self.candle_1h.ret
        if abs(ret) < 1e-8:
            return 0.0
        bias = ret / Config.TREND_SENSITIVITY
        return max(-1.0, min(1.0, bias))

    @property
    def realized_sigma_5m(self) -> float:
        """
        Realized 5-minute return volatility from recent closed candles.

        Uses exponential weighting (half-life = 12 candles ~ 1 hour).
        Bessel-corrected for weighted sample variance.
        Clamped to [0.0005, 0.006] to prevent extremes.
        Falls back to Config.SIGMA_5M when insufficient data.
        """
        if len(self._closed_returns) < self._MIN_CANDLES_FOR_SIGMA:
            return Config.SIGMA_5M
        returns = list(self._closed_returns)
        n = len(returns)

        # Exponential weights
        weights = [2.0 ** ((i - n + 1) / self._SIGMA_HALF_LIFE) for i in range(n)]
        total_w = sum(weights)

        # Weighted mean
        mean = sum(w * r for w, r in zip(weights, returns)) / total_w

        # Weighted variance with Bessel correction
        var = sum(w * (r - mean) ** 2 for w, r in zip(weights, returns)) / (total_w - weights[-1])
        return max(0.0005, min(0.006, math.sqrt(var)))

    @property
    def smoothed_obi(self) -> float:
        """Exponentially-weighted OBI over last ~5 seconds."""
        if not self._obi_history:
            return 0.0
        values = list(self._obi_history)
        n = len(values)
        alpha = 2.0 / (n + 1)
        ema = values[0]
        for v in values[1:]:
            ema = alpha * v + (1 - alpha) * ema
        return ema

    @property
    def weighted_obi(self) -> float:
        """EMA of depth-weighted OBI (more informative than simple OBI)."""
        if not self._wobi_history:
            return 0.0
        values = list(self._wobi_history)
        n = len(values)
        alpha = 2.0 / (n + 1)
        ema = values[0]
        for v in values[1:]:
            ema = alpha * v + (1 - alpha) * ema
        return ema

    @property
    def tick_momentum(self) -> float:
        """
        Tick momentum: ratio of upticks to total ticks over recent history.
        Returns [-1, +1]. Positive = upward momentum.
        """
        if len(self._tick_directions) < 10:
            return 0.0
        ticks = list(self._tick_directions)
        ups = sum(1 for t in ticks if t > 0)
        return (2.0 * ups / len(ticks)) - 1.0

    @property
    def vwap_5m(self) -> Optional[float]:
        """VWAP for the current 5-minute window."""
        if self._vwap_volume < 1e-10:
            return self.book.mid_price
        return self._vwap_price_volume / self._vwap_volume

    @property
    def candle_trend_strength(self) -> float:
        """
        Consecutive candle direction strength.
        Returns [-1, +1]. +1 = all recent candles bullish, -1 = all bearish.
        """
        if len(self._candle_directions) < 3:
            return 0.0
        recent = list(self._candle_directions)[-6:]
        return sum(recent) / len(recent)

    @property
    def volume_ratio(self) -> float:
        """
        Current candle volume / median of recent closed candle volumes.
        > 1.0 = above-average volume. < 1.0 = below-average volume.
        """
        if len(self._closed_volumes) < 3 or self.candle.volume <= 0:
            return 1.0
        sorted_vols = sorted(self._closed_volumes)
        median_vol = sorted_vols[len(sorted_vols) // 2]
        if median_vol <= 0:
            return 1.0
        return self.candle.volume / median_vol

    @property
    def mean_return_5m(self) -> float:
        """Average return of recent closed 5m candles."""
        if len(self._closed_returns) < 3:
            return 0.0
        returns = list(self._closed_returns)
        return sum(returns) / len(returns)

    @property
    def vol_percentile(self) -> float:
        """
        Where current realized sigma sits relative to recent per-candle
        absolute returns.  Returns 0.0-1.0.
        """
        if len(self._closed_returns) < self._MIN_CANDLES_FOR_SIGMA:
            return 0.5
        current = self.realized_sigma_5m
        abs_returns = sorted(abs(r) for r in self._closed_returns)
        rank = sum(1 for r in abs_returns if r <= current)
        return rank / len(abs_returns)

    @property
    def vol_regime(self) -> str:
        """Volatility regime: 'calm', 'normal', or 'storm'."""
        pct = self.vol_percentile
        if pct > 0.90:
            return "storm"
        elif pct < 0.30:
            return "calm"
        return "normal"

    @property
    def last_candle_close_location(self) -> float:
        """Close location of the most recently closed 5m candle."""
        if self._last_closed_candle is None:
            return 0.5
        return self._last_closed_candle.close_location

    def stop(self) -> None:
        self._running = False
