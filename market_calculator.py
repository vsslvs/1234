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
import json as _json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import aiohttp

from config import Config
from ws_orderbook import OrderBookWS

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
WINDOW_SEC = Config.MARKET_WINDOW_SEC  # 300

# Logistic signal steepness.  Maps BTC return → win probability.
#
# k=500 gives nuanced probabilities suitable for two-sided market making:
#   At 0.22% return (1σ): p_up ≈ 0.75  (not saturated)
#   At 0.50% return:       p_up ≈ 0.92
#   At 1.0%  return:       p_up ≈ 0.993
#
# This avoids the k=2000 regime where p_up saturates to 0/1 almost
# immediately, leaving no room for spread-based quoting.
K_SIGNAL: float = 500.0


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
        Fetch BTC 5-minute up/down markets from Gamma API.

        Market slugs follow a deterministic pattern based on window start time:
            btc-updown-5m-{window_start_unix_ts}

        We calculate slugs directly from the current time — no tag/keyword
        search required. Fetch the current window + 5 future windows as a
        lookahead buffer so the bot is never caught without a known market.
        All 6 requests fire concurrently via asyncio.gather.
        """
        now_ts = int(time.time())
        current_ws = (now_ts // WINDOW_SEC) * WINDOW_SEC
        slugs = [
            f"btc-updown-5m-{current_ws + i * WINDOW_SEC}"
            for i in range(6)
        ]

        results = await asyncio.gather(
            *[self._fetch_market_by_slug(s) for s in slugs],
            return_exceptions=True,
        )

        markets = []
        for result in results:
            if isinstance(result, BtcMarket):
                markets.append(result)
                self._markets[result.window_start] = result

        # Memory management: purge market objects older than one window
        cutoff = current_ws - WINDOW_SEC
        for k in [k for k in self._markets if k < cutoff]:
            del self._markets[k]

        log.info("Fetched %d BTC 5m markets from Gamma", len(markets))
        return sorted(markets, key=lambda m: m.window_start)

    async def _fetch_market_by_slug(self, slug: str) -> Optional[BtcMarket]:
        """Fetch a single market by exact slug from Gamma API."""
        try:
            async with self._session.get(
                f"{GAMMA_API}/markets", params={"slug": slug}
            ) as r:
                r.raise_for_status()
                items = await r.json()
        except Exception as exc:
            log.debug("Failed to fetch slug=%s: %s", slug, exc)
            return None
        if not items:
            return None
        item = items[0] if isinstance(items, list) else items
        return self._parse_market(item)

    def _parse_market(self, item: dict) -> Optional[BtcMarket]:
        """
        Parse a Gamma API market dict into a BtcMarket.

        Real Gamma API structure (verified against live market data):
          - item["outcomes"]    = JSON-encoded string: "[\"Up\", \"Down\"]"
          - item["clobTokenIds"] = JSON-encoded string: "[\"id1\", \"id2\"]"
          - outcomes[i] maps to clobTokenIds[i] (parallel arrays)
          - item["slug"]         = "btc-updown-5m-{window_start_ts}"
          - item["endDateIso"]   = date-only string ("2026-03-20") — NOT used

        Window boundaries are extracted from the slug (most reliable source).
        """
        try:
            # Decode parallel JSON-encoded string arrays
            outcomes_raw = item.get("outcomes", "[]")
            clob_ids_raw = item.get("clobTokenIds", "[]")
            outcomes = (
                _json.loads(outcomes_raw)
                if isinstance(outcomes_raw, str)
                else outcomes_raw
            )
            clob_ids = (
                _json.loads(clob_ids_raw)
                if isinstance(clob_ids_raw, str)
                else clob_ids_raw
            )

            if len(outcomes) != 2 or len(clob_ids) != 2:
                return None

            # Map "Up"/"Yes" → yes_token_id,  "Down"/"No" → no_token_id
            yes_token_id: Optional[str] = None
            no_token_id:  Optional[str] = None
            for outcome, token_id in zip(outcomes, clob_ids):
                ol = outcome.lower()
                if ol in ("up", "yes"):
                    yes_token_id = str(token_id)
                elif ol in ("down", "no"):
                    no_token_id = str(token_id)

            if not yes_token_id or not no_token_id:
                return None

            # Derive window boundaries from the deterministic slug
            slug = item.get("slug", "")
            if slug.startswith("btc-updown-5m-"):
                start_ts = int(slug.split("-")[-1])
                end_ts   = start_ts + WINDOW_SEC
            else:
                # Fallback: parse endDateIso and round to 5-minute grid
                end_str = item.get("endDateIso", item.get("end_date_iso", ""))
                if not end_str:
                    return None
                dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                # Round to nearest 5-minute boundary to absorb small timing offsets
                end_ts   = round(int(dt.timestamp()) / WINDOW_SEC) * WINDOW_SEC
                start_ts = end_ts - WINDOW_SEC

            return BtcMarket(
                question_id=str(item.get("id", "")),
                condition_id=str(
                    item.get("conditionId", item.get("condition_id", ""))
                ),
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
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
            # Use the Binance 5-minute candle open as the window reference price.
            # Binance 5m candles share the same UTC-based grid as Polymarket 5m windows
            # (both align to multiples of 300 seconds), so candle.open is the BTC price
            # at window_start — exactly what the signal needs.
            # Fallback to mid only if kline data is unavailable (e.g. at startup).
            candle_open = self._ob_ws.candle.open
            market.open_price = candle_open if candle_open > 0 else mid

        ret = (mid - market.open_price) / market.open_price
        p_up = 1.0 / (1.0 + math.exp(-K_SIGNAL * ret))
        return p_up

    def fair_prices(self, market: BtcMarket) -> tuple[float, float]:
        """
        Returns (fair_yes, fair_no) based on directional signal.
        fair_yes + fair_no should equal ~1.0.
        """
        p_up = self.p_up_signal(market)
        return p_up, 1.0 - p_up

    def dynamic_spread(self, market: BtcMarket) -> float:
        """
        Dynamic spread that narrows as the window approaches close.

        Early (300s left): spread = base × 1.5  (high uncertainty)
        Late  (10s left):  spread = base × 0.53 (direction clearer)

        Returns spread in price units (e.g. 0.03 = 3 cents).
        """
        time_frac = market.seconds_to_close / Config.MARKET_WINDOW_SEC  # 1.0→0.0
        time_scale = 0.5 + time_frac  # 1.5 → 0.5
        base = Config.BASE_SPREAD_BPS / 10_000
        spread = base * time_scale

        min_s = Config.MIN_SPREAD_BPS / 10_000
        max_s = Config.MAX_SPREAD_BPS / 10_000
        return max(min_s, min(max_s, spread))

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
