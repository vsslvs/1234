"""
5-minute BTC up/down market calculator for Polymarket.

Polymarket creates 288 BTC markets per day, one per 5-minute window.
Each market resolves YES if BTC closes higher than it opened during that window.

Market window boundaries (UTC):
    window_index = floor(unix_time / 300)
    window_start = window_index * 300
    window_end   = window_start + 300

Signal model (random-walk CDF)
------------------------------
We estimate P(BTC closes UP) using the standard normal CDF (Φ):

    p_up = Φ( ret / σ_remaining )

where:
    ret          = (mid - open) / open          (return so far)
    σ_remaining  = σ₅ × √(stc / WINDOW_SEC)    (vol of remaining time)
    σ₅           = Config.SIGMA_5M ≈ 0.22%      (BTC 5-min vol)

This is time-adjusted: the same BTC return gives a MUCH higher p_up
near the end of the window (less time for reversal) than at the start.

Examples with σ₅ = 0.22%:
    ret=+0.22%, stc=290s: p_up = Φ(1.02) = 0.846  (moderate confidence)
    ret=+0.22%, stc= 10s: p_up = Φ(5.47) = 1.000  (near certain)
    ret=+0.05%, stc= 30s: p_up = Φ(0.72) = 0.764  (mild lean)

Usage:
    calc = MarketCalculator(btc_ws)
    market = calc.current_market()
    p_up = calc.p_up_signal(market)
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

_SQRT2 = math.sqrt(2.0)


def _phi(z: float) -> float:
    """Standard normal CDF: Φ(z) = 0.5 × (1 + erf(z/√2))."""
    return 0.5 * (1.0 + math.erf(z / _SQRT2))


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
    # Directional signal (time-adjusted)
    # ------------------------------------------------------------------

    def p_up_signal(self, market: BtcMarket) -> float:
        """
        Estimate P(BTC closes UP) for the current window.

        Uses a random-walk model:
            p_up = Φ( ret / σ_remaining )

        where σ_remaining = σ₅ × √(seconds_to_close / 300).
        The same BTC return gives HIGHER p_up near close (less time
        for reversal) — this is the key improvement over a fixed-K logistic.
        """
        mid = self._ob_ws.book.mid_price
        if mid is None:
            return 0.5

        if market.open_price is None:
            candle_open = self._ob_ws.candle.open
            market.open_price = candle_open if candle_open > 0 else mid

        ret = (mid - market.open_price) / market.open_price

        stc = market.seconds_to_close
        if stc <= 0.5:
            # Window essentially closed — return certainty
            return 1.0 if ret > 0 else (0.0 if ret < 0 else 0.5)

        sigma_remaining = Config.SIGMA_5M * math.sqrt(stc / WINDOW_SEC)
        if sigma_remaining < 1e-10:
            return 0.5

        z = ret / sigma_remaining
        # Clamp z to avoid extreme probabilities that cause numerical issues
        z = max(-6.0, min(6.0, z))
        return _phi(z)

    def fair_prices(self, market: BtcMarket) -> tuple[float, float]:
        """
        Returns (fair_yes, fair_no) based on directional signal.
        fair_yes + fair_no = 1.0.
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
