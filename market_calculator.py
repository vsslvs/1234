"""
5-minute BTC up/down market calculator for Polymarket.

Polymarket creates 288 BTC markets per day, one per 5-minute window.
Each market resolves YES if BTC closes higher than it opened during that window.

Market window boundaries (UTC):
    window_index = floor(unix_time / 300)
    window_start = window_index * 300
    window_end   = window_start + 300

This module provides:
1. Timing helpers (seconds to window close, is it entry/exit window)
2. A lightweight Gamma API client to fetch today's market token IDs
3. A composite signal that estimates probability of UP using:
   - Logistic curve on BTC return (primary)
   - Order-flow imbalance (confirmation)
   - Multi-timeframe 1m candle (filter)
   - Adaptive K calibrated to realized volatility
4. Fee-adjusted fair pricing
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

# Logistic signal steepness — fallback value.
#
# Calibration (random-walk model):
#   σ₅    = 0.22%   (BTC 5-min vol: 60% annual → 5-min window)
#   σ_rem = σ₅ × √(ENTRY_WINDOW_SEC / WINDOW_SEC)
#         = 0.22% × √(10/300) = 0.040%
#
#   For p=0.94 to equal true P at entry:
#     k_exact  = logit(0.94) / (Φ⁻¹(0.94) × σ_rem) = 2.75 / (1.555×0.040%) ≈ 4 421
#
#   k_safe = 2 000 chosen for robustness up to σ₅=0.50% (high-vol day).
#   VOLATILITY_GATE_BPS=200 blocks windows where σ₅ > 0.70%,
#   ensuring positive EV (actual P > break-even 0.92) across all traded windows.
#
#   Win rates with k=2000, threshold=0.94:
#     σ₅ = 0.22% → P = 99.97%  (typical day)
#     σ₅ = 0.50% → P = 93.6%   (high-vol,  EV > 0)
#     σ₅ > 0.70% → not traded  (VOLATILITY_GATE blocks)
K_SIGNAL_DEFAULT: float = 2000.0

# Clamp bounds for adaptive K
K_SIGNAL_MIN: float = 500.0
K_SIGNAL_MAX: float = 8000.0

# Re-export for backward compatibility
K_SIGNAL = K_SIGNAL_DEFAULT

# Multi-timeframe confidence reduction when 1m contradicts 5m
_MTF_PENALTY: float = 0.20  # reduce confidence by 20%


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


def _phi_inv(p: float) -> float:
    """Approximate inverse standard normal CDF (probit function).

    Uses the rational approximation from Abramowitz & Stegun.
    Accurate to ~4.5 × 10⁻⁴ for 0.01 ≤ p ≤ 0.99.
    """
    if p <= 0 or p >= 1:
        return 0.0
    # Symmetry: if p > 0.5, use 1-p
    if p > 0.5:
        return -_phi_inv(1.0 - p)
    t = math.sqrt(-2.0 * math.log(p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return -(t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t))


class MarketCalculator:
    """
    Fetches live Polymarket 5-min BTC market metadata and computes
    the directional signal (probability of UP) from the Binance price feed.

    Signal components:
      1. Logistic return signal (primary) with adaptive K
      2. Order-flow imbalance (confirmation, weight = OFI_WEIGHT)
      3. 1-minute candle trend (filter — penalizes contradictions)
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
    # Adaptive signal calibration
    # ------------------------------------------------------------------

    def adaptive_k(self) -> float:
        """
        Compute logistic steepness K from realized volatility.

        K = logit(threshold) / (Φ⁻¹(threshold) × σ_remaining)

        Where σ_remaining = σ_5m_realized × √(entry_window_sec / window_sec)

        In low-vol regimes K increases (signal is more confident per unit
        of return). In high-vol regimes K decreases (same return is less
        meaningful). Clamped to [K_SIGNAL_MIN, K_SIGNAL_MAX].
        """
        sigma_5m = self._ob_ws.realized_vol_5m
        if sigma_5m <= 0:
            return K_SIGNAL_DEFAULT

        sigma_remaining = sigma_5m * math.sqrt(
            Config.ENTRY_WINDOW_SEC / Config.MARKET_WINDOW_SEC
        )
        if sigma_remaining <= 0:
            return K_SIGNAL_DEFAULT

        threshold = 0.94  # P_UP_THRESHOLD
        logit_t = math.log(threshold / (1.0 - threshold))
        phi_inv_t = _phi_inv(threshold)

        if phi_inv_t == 0:
            return K_SIGNAL_DEFAULT

        k = logit_t / (phi_inv_t * sigma_remaining)
        return max(K_SIGNAL_MIN, min(K_SIGNAL_MAX, k))

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

    def get_markets_snapshot(self) -> list[dict]:
        """Return all known markets as dicts with URLs for the dashboard."""
        result = []
        for ws, m in sorted(self._markets.items()):
            slug = f"btc-updown-5m-{m.window_start}"
            sec = m.seconds_to_close
            if m.is_expired:
                status = "expired"
            elif m.is_entry_window:
                status = "entry"
            elif sec > 0:
                status = "active" if ws == current_window_start() else "upcoming"
            else:
                status = "expired"
            result.append({
                "window_start": m.window_start,
                "window_end": m.window_end,
                "slug": slug,
                "url": f"https://polymarket.com/event/{slug}",
                "status": status,
                "seconds_to_close": round(sec, 0),
            })
        return result

    def current_market(self) -> Optional[BtcMarket]:
        """Return the market for the current 5-minute window, if known."""
        ws = current_window_start()
        return self._markets.get(ws)

    # ------------------------------------------------------------------
    # Directional signal — composite
    # ------------------------------------------------------------------

    def p_up_signal(self, market: BtcMarket) -> float:
        """
        Estimate P(BTC closes UP) for the current window.

        Composite signal:
          1. Logistic on return (primary) — adaptive K from realized vol
          2. Order-flow imbalance (secondary) — bid/ask volume skew
          3. Multi-timeframe filter — 1m candle contradiction penalty

        Returns 0.5 (neutral) if data is stale or unavailable.
        """
        mid = self._ob_ws.book.mid_price
        if mid is None:
            return 0.5

        # Staleness check — don't trade on old data
        if self._ob_ws.book.is_stale:
            return 0.5

        if market.open_price is None:
            # Use the Binance 5-minute candle open as the window reference price.
            candle_open = self._ob_ws.candle.open
            market.open_price = candle_open if candle_open > 0 else mid

        # 1. Primary: logistic return signal with adaptive K
        ret = (mid - market.open_price) / market.open_price
        k = self.adaptive_k()
        exponent = -k * ret
        # Clamp to avoid OverflowError on extreme returns
        exponent = max(-500, min(500, exponent))
        p_logistic = 1.0 / (1.0 + math.exp(exponent))

        # 2. Secondary: order-flow imbalance blending
        ofi = self._ob_ws.book.order_flow_imbalance
        ofi_weight = Config.OFI_WEIGHT

        # OFI adjustment is largest near p=0.5 (uncertain) and smallest
        # at extremes. This is correct: OFI helps resolve ambiguity,
        # but should not override a strong price-return signal.
        uncertainty = 0.5 - abs(p_logistic - 0.5)
        p_combined = p_logistic + ofi_weight * ofi * uncertainty

        # 3. Multi-timeframe: 1m candle contradiction penalty
        candle_1m_dir = self._ob_ws.candle_1m.direction
        if candle_1m_dir != 0:
            # Signal direction from logistic
            signal_dir = 1 if p_combined > 0.5 else -1
            if candle_1m_dir != signal_dir:
                # 1m candle contradicts — reduce confidence toward 0.5
                p_combined = p_combined + _MTF_PENALTY * (0.5 - p_combined)

        # Clamp to [0.01, 0.99] — never fully certain
        return max(0.01, min(0.99, p_combined))

    def fair_prices(self, market: BtcMarket) -> tuple[float, float]:
        """
        Returns (fair_yes, fair_no) based on directional signal.

        Fee-adjusted: subtracts taker fee from raw probabilities.
        This accounts for the cost of being filled by an informed taker
        (adverse selection cost) and makes entry criteria more conservative.
        """
        p_up = self.p_up_signal(market)
        fee_yes = self.taker_fee(p_up)
        fee_no = self.taker_fee(1.0 - p_up)
        fair_yes = p_up - fee_yes
        fair_no = (1.0 - p_up) - fee_no
        return fair_yes, fair_no

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

    def dynamic_min_edge(self) -> float:
        """
        Scale minimum edge requirement with realized volatility.
        Higher vol = require more edge. Capped between 30 and 150 bps.
        """
        sigma_5m = self._ob_ws.realized_vol_5m
        sigma_default = 0.0022
        if sigma_default <= 0:
            return Config.MIN_EDGE_BPS
        ratio = sigma_5m / sigma_default
        return max(30, min(150, Config.MIN_EDGE_BPS * ratio))
