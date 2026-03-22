"""
Polymarket BTC 5-minute market maker.

Strategy
--------
For each 5-minute BTC window:

1. Look up the Polymarket market for the current window.
2. Compute fair YES / NO prices using Binance mid-price vs window-open price.
3. During the ENTRY WINDOW (last ENTRY_WINDOW_SEC seconds before close):
   - Quote YES at TARGET_PRICE_YES  (e.g. 0.92) if P(up) > 0.80
   - Quote NO  at TARGET_PRICE_NO   (e.g. 0.92) if P(up) < 0.20
   - (High-confidence signal only — avoid quoting around 50%)
4. Every QUOTE_REFRESH_MS ms, check if price has drifted enough to warrant
   a cancel/replace cycle.
5. 2 seconds before close, cancel all open orders to avoid fills on an
   already-known outcome.

Why only high-confidence entry?
- Taker fee at p=0.50 is ~1.56%. Even as a maker (0 fee), if you quote
  at 0.92 on the losing side, you lose $0.92 per share. You need the
  signal to be right >92% of the time.
- At ±0.3% BTC move in a 5m window the logistic signal gives ~85%.
  Combined with the discount rebate, EV is positive.
- Markets near 50% have the most adverse-selection risk from faster bots.

Rebate mechanic
---------------
Polymarket pays USDC rebates to makers funded by taker fees.
We don't model the exact rebate here — it is paid out daily and adds
to P&L on top of the spread captured at resolution.

Cancel/replace < 100 ms
-----------------------
We fire cancel + new-place concurrently via asyncio.gather.
On a VPS co-located with Cloudflare/Polygon infrastructure (EU or US-East),
RTT to Polymarket CLOB is typically 10-30 ms.
asyncio.gather brings total wall-clock time to max(cancel_rtt, place_rtt).
"""
import asyncio
import logging
import time
from typing import Dict, Optional

from config import Config
from dashboard import EventBus
from market_calculator import BtcMarket, MarketCalculator, K_SIGNAL
from polymarket_client import MakerOrder, PolymarketClient, SIDE_BUY
from stats import BotStats
from ws_orderbook import OrderBookWS

log = logging.getLogger(__name__)

# Minimum price change to trigger a cancel/replace (avoids churn)
PRICE_DRIFT_THRESHOLD = 0.005   # 0.5 cents on a ~92-cent order
# Probability threshold: only quote if signal is this strong.
# For positive EV buying YES at TARGET_PRICE_YES=0.92 we need p_up > 0.92.
# Using 0.94 gives ~200 bps expected edge at entry, providing a safety margin.
P_UP_THRESHOLD   = 0.94
P_DOWN_THRESHOLD = 0.06


class MarketSide:
    """Holds the live maker order for one side (YES or NO) of one market."""

    def __init__(self, token_id: str, side_label: str):
        self.token_id   = token_id
        self.side_label = side_label  # "YES" or "NO"
        self.order: Optional[MakerOrder] = None

        # Stats fields — survive order cancellation so _evaluate_and_record_window
        # can read them at rollover time even after EXIT_WINDOW cancel.
        self.was_ever_active:    bool  = False   # True if an order was placed this window
        self.p_signal_at_entry:  float = 0.0     # logistic p when first order was placed
        self.last_entry_price:   float = 0.0     # price of first order this window

    @property
    def has_order(self) -> bool:
        return self.order is not None

    def price_drifted(self, new_price: float) -> bool:
        if not self.order:
            return False
        return abs(self.order.price - new_price) > PRICE_DRIFT_THRESHOLD


class WindowState:
    """All open orders for one 5-minute window."""

    def __init__(self, market: BtcMarket):
        self.market = market
        self.yes = MarketSide(market.yes_token_id, "YES")
        self.no  = MarketSide(market.no_token_id,  "NO")

    def all_orders(self) -> list[MakerOrder]:
        orders = []
        if self.yes.order:
            orders.append(self.yes.order)
        if self.no.order:
            orders.append(self.no.order)
        return orders


class MarketMaker:
    """
    Orchestrates the quoting loop for Polymarket BTC 5-minute markets.

    One WindowState is active at a time, matching the current 5-minute window.
    When the window rolls over, the old state is cleaned up and the new one
    is created.
    """

    def __init__(
        self,
        client: PolymarketClient,
        calc: MarketCalculator,
        ob_ws: OrderBookWS,
        event_bus: Optional["EventBus"] = None,
    ):
        self._client  = client
        self._calc    = calc
        self._ob_ws   = ob_ws
        self._bus     = event_bus
        self._state:  Optional[WindowState] = None
        self._running = False
        self._stats   = BotStats()
        self._windows_since_stats_log = 0
        self._last_state_push: float = 0.0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        log.info("MarketMaker starting")

        await self._client.check_approvals()
        await self._refresh_market_list()

        # Pre-fetch upcoming markets once, then periodically in background
        asyncio.create_task(self._market_refresh_loop(), name="market-refresh")

        interval = Config.QUOTE_REFRESH_MS / 1000
        while self._running:
            t0 = time.monotonic()
            try:
                await self._tick()
            except Exception as exc:
                log.error("tick error: %s", exc, exc_info=True)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def stop(self) -> None:
        self._running = False
        self._stats.log_summary(
            k=K_SIGNAL,
            entry_window_sec=Config.ENTRY_WINDOW_SEC,
            market_window_sec=Config.MARKET_WINDOW_SEC,
            threshold=P_UP_THRESHOLD,
            entry_price=Config.TARGET_PRICE_YES,
            size_usdc=Config.ORDER_SIZE_USDC,
        )
        await self._cancel_all_open()

    # ------------------------------------------------------------------
    # Per-tick logic
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        market = self._calc.current_market()
        if market is None:
            return

        # Roll over state when window changes
        if self._state is None or self._state.market.window_start != market.window_start:
            await self._rollover(market)

        state = self._state
        if state is None:
            return

        # Push state snapshot to dashboard (throttled to ~1/sec)
        self._push_dashboard_state(market)

        # Exit window: cancel everything and wait for resolution
        if market.seconds_to_close <= Config.EXIT_WINDOW_SEC:
            await self._cancel_window(state)
            return

        # Only quote during entry window
        if not market.is_entry_window:
            return

        await self._quote_window(state, market)

    async def _rollover(self, new_market: BtcMarket) -> None:
        """Clean up old window, evaluate its outcome, set up new window state."""
        if self._state:
            # Evaluate BEFORE cancel so was_ever_active / entry fields are intact
            self._evaluate_and_record_window(self._state)
            log.info("Window rolled over — cancelling old orders")
            await self._cancel_window(self._state)

            self._windows_since_stats_log += 1
            if self._windows_since_stats_log >= Config.STATS_LOG_INTERVAL:
                self._windows_since_stats_log = 0
                self._stats.log_summary(
                    k=K_SIGNAL,
                    entry_window_sec=Config.ENTRY_WINDOW_SEC,
                    market_window_sec=Config.MARKET_WINDOW_SEC,
                    threshold=P_UP_THRESHOLD,
                    entry_price=Config.TARGET_PRICE_YES,
                    size_usdc=Config.ORDER_SIZE_USDC,
                )

        self._state = WindowState(new_market)
        log.info(
            "New window: %s → %s",
            new_market.window_start,
            new_market.window_end,
        )

    async def _quote_window(self, state: WindowState, market: BtcMarket) -> None:
        """Place or refresh maker orders based on directional signal."""
        # --- Volatility gate -------------------------------------------
        # Skip trading if the recent Binance 5-minute candle range is extreme.
        # A high-low range > VOLATILITY_GATE_BPS signals a flash-crash, major
        # news event, or severely illiquid conditions where the random-walk model
        # breaks down and EV can turn negative.
        candle_vol = self._ob_ws.candle.volatility_bps
        if candle_vol > Config.VOLATILITY_GATE_BPS:
            log.debug(
                "Vol gate: window=%d  candle_vol=%.0f bps > gate=%.0f bps — skip",
                state.market.window_start, candle_vol, Config.VOLATILITY_GATE_BPS,
            )
            await self._cancel_window(state)
            return

        # fair_prices() calls p_up_signal() internally — no duplicate call needed.
        # fair_prices returns (p_up, 1-p_up), so fair_yes == p_up by definition.
        fair_yes, fair_no = self._calc.fair_prices(market)
        p_up = fair_yes

        tasks = []

        # ----- YES side: buy UP token if strong upside signal -----
        if p_up > P_UP_THRESHOLD:
            target = Config.TARGET_PRICE_YES
            # BUY edge = (fair - target) × 10000.
            # Positive means we are buying BELOW our estimated fair value.
            edge = (fair_yes - target) * 10_000
            if edge >= Config.MIN_EDGE_BPS:
                # Record entry stats on the FIRST order of this window only
                if not state.yes.was_ever_active:
                    state.yes.p_signal_at_entry = p_up
                    state.yes.last_entry_price  = target
                    state.yes.was_ever_active   = True
                tasks.append(self._refresh_side(state.yes, target))
        else:
            if state.yes.has_order:
                tasks.append(self._cancel_side(state.yes))

        # ----- NO side: buy DOWN token if strong downside signal -----
        if p_up < P_DOWN_THRESHOLD:
            target = Config.TARGET_PRICE_NO
            # BUY edge for NO: (fair_no - target) × 10000
            edge = (fair_no - target) * 10_000
            if edge >= Config.MIN_EDGE_BPS:
                if not state.no.was_ever_active:
                    state.no.p_signal_at_entry = p_up
                    state.no.last_entry_price  = target
                    state.no.was_ever_active   = True
                tasks.append(self._refresh_side(state.no, target))
        else:
            if state.no.has_order:
                tasks.append(self._cancel_side(state.no))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _evaluate_and_record_window(self, state: WindowState) -> None:
        """
        Approximate trade outcome for the closing window and record it.

        Resolution approximation:
          actual_up = (Binance mid at rollover) >= (window open price)
        This proxies the Chainlink oracle resolution.  Correlation between
        Binance and Chainlink BTC/USD is >99.9% on 5-minute scales.

        We record only sides that were ever active (had an order placed).
        The stats fields (was_ever_active, p_signal_at_entry, last_entry_price)
        survive the EXIT_WINDOW cancel, so they are readable here at rollover.
        """
        market = state.market
        mid    = self._ob_ws.book.mid_price
        if mid is None or market.open_price is None:
            return  # no price data — skip recording for this window

        btc_closed_up = mid >= market.open_price

        for side in (state.yes, state.no):
            if not side.was_ever_active:
                continue
            # YES token wins if BTC closed up; NO token wins if BTC closed down
            signal_is_up = (side.side_label == "YES")
            won = (btc_closed_up == signal_is_up)
            self._stats.record_trade(
                window_start=market.window_start,
                side=side.side_label,
                entry_price=side.last_entry_price,
                size_usdc=Config.ORDER_SIZE_USDC,
                p_signal=side.p_signal_at_entry,
                won=won,
            )
            # Emit trade event to dashboard
            if self._bus:
                shares = Config.ORDER_SIZE_USDC / side.last_entry_price
                pnl = shares * (1.0 - side.last_entry_price) if won else -Config.ORDER_SIZE_USDC
                self._bus.push_trade({
                    "window_start": market.window_start,
                    "side": side.side_label,
                    "entry_price": side.last_entry_price,
                    "p_signal": side.p_signal_at_entry,
                    "won": won,
                    "pnl": round(pnl, 2),
                })

    # ------------------------------------------------------------------
    # Dashboard state push
    # ------------------------------------------------------------------

    def _push_dashboard_state(self, market: BtcMarket) -> None:
        if not self._bus:
            return
        now = time.monotonic()
        if now - self._last_state_push < 1.0:
            return
        self._last_state_push = now

        self._bus.push_state({
            "status": "running" if self._running else "stopped",
            "btc_price": self._ob_ws.book.mid_price,
            "uptime_sec": round(time.time() - self._stats._session_start, 0),
            "wallet": Config.PRIVATE_KEY[:6] + "..." + Config.PRIVATE_KEY[-4:],
            "current_window": market.window_start,
            "seconds_to_close": round(market.seconds_to_close, 0),
            "is_entry_window": market.is_entry_window,
            "stats": self._stats.to_dict(),
            "markets": self._calc.get_markets_snapshot(),
        })

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    async def _refresh_side(self, side: MarketSide, target_price: float) -> None:
        """Place a new order or cancel/replace if price drifted."""
        if not side.has_order:
            # Fresh placement
            try:
                order = await self._client.place_maker_order(
                    token_id=side.token_id,
                    side=SIDE_BUY,        # buying the outcome token
                    price=target_price,
                    size_usdc=Config.ORDER_SIZE_USDC,
                )
                side.order = order
            except Exception as exc:
                log.error("place_maker_order %s failed: %s", side.side_label, exc)
        elif side.price_drifted(target_price):
            # Cancel/replace with new price
            t0 = time.monotonic()
            new_order = await self._client.cancel_replace(
                old_order=side.order,
                new_price=target_price,
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
            log.debug(
                "cancel_replace %s %.4f→%.4f in %.1f ms",
                side.side_label, side.order.price, target_price, elapsed_ms,
            )
            side.order = new_order  # None if both requests failed

    async def _cancel_side(self, side: MarketSide) -> None:
        if side.order:
            await self._client.cancel_order(side.order.order_id)
            side.order = None

    async def _cancel_window(self, state: WindowState) -> None:
        tasks = []
        if state.yes.order:
            tasks.append(self._cancel_side(state.yes))
        if state.no.order:
            tasks.append(self._cancel_side(state.no))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Cancelled all orders for window %s", state.market.window_start)

    async def _cancel_all_open(self) -> None:
        if self._state:
            await self._cancel_window(self._state)
        await self._client.cancel_all_orders()

    # ------------------------------------------------------------------
    # Background market list refresh (every 10 minutes)
    # ------------------------------------------------------------------

    async def _market_refresh_loop(self) -> None:
        # Sleep FIRST so the initial fetch in run() is not immediately duplicated.
        while self._running:
            await asyncio.sleep(600)
            if self._running:
                await self._refresh_market_list()

    async def _refresh_market_list(self) -> None:
        markets = await self._calc.fetch_upcoming_markets()
        if not markets:
            log.warning("No BTC 5m markets found — will retry on next refresh cycle")
        else:
            log.debug("Market list refreshed: %d markets", len(markets))
