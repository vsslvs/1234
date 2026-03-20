"""
5-minute BTC up/down market calculator for Polymarket.

Polymarket creates 288 BTC markets per day, one per 5-minute window.
Each market resolves YES if BTC closes higher than it opened during that window.

Market window boundaries (UTC):
    window_index = floor(unix_time / 300)
    window_start = window_index * 300
    window_end   = window_start + 300

The token IDs for YES / NO sides of each market can be derived from the
question text or fetched from the Gamma API. This module provides:

1. Timing helpers (seconds to window close, is it entry/exit window)
2. A lightweight Gamma API client to fetch today's market token IDs
3. A btc_direction_signal() function that estimates probability of UP
   based on current Binance price vs window-open price

Usage:
    calc = MarketCalculator(btc_ws)
    market = await calc.current_market()
    p_up = calc.p_up_signal()
"""
import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp

from config import Config
from ws_orderbook import OrderBookWS

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
WINDOW_SEC = Config.MARKET_WINDOW_SEC  # 300


@dataclass
class BtcMarket:
    """One 5-minute BTC up/down market."""
    question_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    window_start: int    # unix timestamp
    window_end: int
    open_price: Optional[float] = None   # BTC price at window open

    @property
    def seconds_to_close(self) -> float:
        return max(0.0, self.window_end - time.time())

    @property
    def is_entry_window(self) -> bool:
        """True during the last ENTRY_WINDOW_SEC before close."""
        s = self.seconds_to_close
        return Config.EXIT_WINDOW_SEC < s <= Config.ENTRY_WINDOW_SEC

    @property
    def is_expired(self) -> bool:
        return time.time() > self.window_end


def current_window_start() -> int:
    return int(time.time() // WINDOW_SEC) * WINDOW_SEC


def current_window_end() -> int:
    return current_window_start() + WINDOW_SEC


def seconds_to_next_window() -> float:
    return current_window_end() - time.time()


class MarketCalculator:
    """
    Fetches live Polymarket 5-min BTC market metadata and computes
    the directional signal (probability of UP) from the Binance price feed.
    """

    def __init__(self, ob_ws: OrderBookWS):
        self._ob_ws = ob_ws
        self._markets: Dict[int, BtcMarket] = {}     # window_start → market
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        )
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # Market discovery via Gamma API
    # ------------------------------------------------------------------

    async def fetch_upcoming_markets(self) -> List[BtcMarket]:
        """
        Query Gamma for active BTC 5-minute up/down markets.
        Returns markets sorted by window_start ascending.
        """
        params = {
            "tag":    "btc-5m",
            "active": "true",
            "limit":  "50",
        }
        try:
            async with self._session.get(f"{GAMMA_API}/markets", params=params) as r:
                r.raise_for_status()
                items = await r.json()
        except Exception as exc:
            log.error("Failed to fetch markets from Gamma: %s", exc)
            return []

        markets = []
        for item in items:
            market = self._parse_market(item)
            if market:
                markets.append(market)
                self._markets[market.window_start] = market

        log.info("Fetched %d BTC 5m markets from Gamma", len(markets))
        return sorted(markets, key=lambda m: m.window_start)

    def _parse_market(self, item: dict) -> Optional[BtcMarket]:
        try:
            tokens = item.get("tokens", [])
            yes_token = next((t for t in tokens if t.get("outcome") == "Yes"), None)
            no_token  = next((t for t in tokens if t.get("outcome") == "No"),  None)
            if not yes_token or not no_token:
                return None

            # Parse window boundaries from market end time
            end_ts = int(item.get("endDateIso", item.get("end_date_iso", 0)))
            if end_ts == 0:
                return None
            start_ts = end_ts - WINDOW_SEC

            return BtcMarket(
                question_id=str(item.get("id", "")),
                condition_id=str(item.get("conditionId", item.get("condition_id", ""))),
                yes_token_id=str(yes_token["tokenId"]),
                no_token_id=str(no_token["tokenId"]),
                window_start=start_ts,
                window_end=end_ts,
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.debug("Could not parse market item: %s | %s", exc, item)
            return None

    # ------------------------------------------------------------------
    # Current market
    # ------------------------------------------------------------------

    def current_market(self) -> Optional[BtcMarket]:
        """Return the market for the current 5-minute window, if known."""
        ws = current_window_start()
        return self._markets.get(ws)

    # ------------------------------------------------------------------
    # Directional signal
    # ------------------------------------------------------------------

    def p_up_signal(self, market: BtcMarket) -> float:
        """
        Estimate P(BTC closes UP) for the current window.

        Method: compare current mid-price to the window-open price.
        - If we don't have the window-open price yet, record it now.
        - Return a probability in [0, 1] using a logistic curve on the
          return magnitude, tuned so that ±0.3% move → ~85% confidence.

        This is a simple signal. In production you would blend this with
        order-flow imbalance, vol surface, etc.
        """
        mid = self._ob_ws.book.mid_price
        if mid is None:
            return 0.5

        if market.open_price is None:
            market.open_price = mid
            return 0.5

        ret = (mid - market.open_price) / market.open_price
        # logistic: k=1500 gives ~85% at ret=0.3%
        k = 1500.0
        p_up = 1.0 / (1.0 + math.exp(-k * ret))
        return p_up

    def fair_prices(self, market: BtcMarket) -> tuple[float, float]:
        """
        Returns (fair_yes, fair_no) based on directional signal.
        fair_yes + fair_no should equal ~1.0.
        """
        p_up = self.p_up_signal(market)
        return p_up, 1.0 - p_up

    def taker_fee(self, p: float) -> float:
        """
        Dynamic taker fee formula: C × 0.25 × (p × (1-p))²
        where C is normalised so max fee at p=0.5 is ~0.0156 (1.56%).
        C = 1.0 gives max = 0.25 × (0.5×0.5)² = 0.25 × 0.0625 = 0.015625.
        """
        return 0.25 * (p * (1 - p)) ** 2

    def edge_bps(self, fair: float, quoted: float, p: float) -> float:
        """
        Expected edge in basis points for a maker order.
        edge = (quoted - fair) × 10000  — positive means we're quoting
        above our fair value (good for a sell, bad for a buy).
        We only place orders where |edge| > MIN_EDGE_BPS.
        """
        return (quoted - fair) * 10_000
