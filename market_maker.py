"""
Polymarket BTC 5-minute market maker.

Strategy
--------
For each 5-minute BTC window:

1. Look up the Polymarket market for the current window.
2. Compute fair YES / NO prices using Binance mid-price vs window-open price.
3. During the ENTRY WINDOW (last ENTRY_WINDOW_SEC seconds before close):
   - Quote YES at market ask (capped at MAX_ENTRY_PRICE) if p_up > 0.5,
     edge ≥ MIN_EDGE_BPS, and p_up > entry_price + BREAKEVEN_SAFETY_BPS
   - Quote NO  only if p_up < 0.5 (signal favours DOWN) and same edge checks
   - Dynamic pricing adapts to real orderbook conditions
4. Every QUOTE_REFRESH_MS ms, check if price has drifted enough to warrant
   a cancel/replace cycle.
5. 2 seconds before close, cancel all open orders to avoid fills on an
   already-known outcome.

Dynamic entry pricing
---------------------
Entry price is the real Polymarket ask (capped at MAX_ENTRY_PRICE).
Two conditions must hold:
  1. Edge = (fair_price - entry_price) ≥ MIN_EDGE_BPS
  2. Break-even safety: p_signal > entry_price + BREAKEVEN_SAFETY_BPS/10000
This replaces the old fixed TARGET_PRICE_YES=0.92 approach, which gave
unrealistic fills in paper mode and wouldn't fill in live trading.

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

from bot_state import state as dashboard_state, TradeSnapshot
from config import Config
from market_calculator import BtcMarket, MarketCalculator, K_SIGNAL
from polymarket_client import MakerOrder, PolymarketClient, SIDE_BUY
from stats import BotStats
from ws_orderbook import OrderBookWS

log = logging.getLogger(__name__)

# Minimum price change to trigger a cancel/replace (avoids churn)
PRICE_DRIFT_THRESHOLD = 0.005   # 0.5 cents


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
    ):
        self._client  = client
        self._calc    = calc
        self._ob_ws   = ob_ws
        self._state:  Optional[WindowState] = None
        self._running = False
        self._stats   = BotStats()
        self._windows_since_stats_log = 0
        self._last_status_log: float = 0.0  # monotonic time of last status log
        self._last_entry_log: float = 0.0   # monotonic time of last entry window log

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

    async def swap_client(self, new_client) -> None:
        """Hot-swap the trading client (paper ↔ live) without stopping the bot."""
        log.info("Swapping client → %s", type(new_client).__name__)
        # Cancel all orders on the old client
        if self._state:
            await self._cancel_window(self._state)
            self._state = None
        await self._client.cancel_all_orders()
        self._client = new_client
        await self._client.check_approvals()
        is_paper = hasattr(new_client, 'resolve_trade')
        dashboard_state.paper_trading = is_paper
        log.info("Client swapped to %s mode", "PAPER" if is_paper else "LIVE")

    async def stop(self) -> None:
        self._running = False
        self._stats.log_summary(
            k=K_SIGNAL,
            entry_window_sec=Config.ENTRY_WINDOW_SEC,
            market_window_sec=Config.MARKET_WINDOW_SEC,
            max_entry_price=Config.MAX_ENTRY_PRICE,
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

        # --- Stale data guard --------------------------------------------
        # If the Binance WS feed hasn't updated for STALE_DATA_MAX_SEC,
        # the mid-price is outdated.  Skip trading to avoid orders based
        # on prices that may be minutes (or hours) old.
        book_age = time.time() - self._ob_ws.book.last_update_ms / 1000
        if book_age > Config.STALE_DATA_MAX_SEC:
            if market.is_entry_window:
                log.warning(
                    "Stale data SKIP | book age=%.1fs > limit=%.0fs — not trading",
                    book_age, Config.STALE_DATA_MAX_SEC,
                )
            return

        # Determine phase and update dashboard
        if market.seconds_to_close <= Config.EXIT_WINDOW_SEC:
            phase = "exit"
        elif market.is_entry_window:
            phase = "entry"
        else:
            phase = "waiting"

        self._update_dashboard(market, state, phase)

        # Exit window: cancel everything and wait for resolution
        if phase == "exit":
            await self._cancel_window(state)
            return

        # Only quote during entry window
        if phase == "waiting":
            now = time.monotonic()
            if now - self._last_status_log >= 30.0:
                self._last_status_log = now
                mid = self._ob_ws.book.mid_price
                stc = market.seconds_to_close
                p_up = self._calc.p_up_signal(market)
                log.info(
                    "Waiting | BTC=%.2f  p_up=%.4f  window_close_in=%.0fs  entry_in=%.0fs",
                    mid or 0, p_up, stc, max(0, stc - Config.ENTRY_WINDOW_SEC),
                )
            return

        await self._quote_window(state, market)

    def _update_dashboard(self, market: BtcMarket, state: WindowState, phase: str) -> None:
        """Push current state to the shared dashboard object."""
        mid = self._ob_ws.book.mid_price or 0.0
        p_up = self._calc.p_up_signal(market)

        ds = dashboard_state
        ds.btc_price = mid
        ds.btc_open_price = market.open_price or 0.0
        ds.p_up = p_up
        ds.fair_yes = p_up
        ds.fair_no = 1.0 - p_up
        ds.candle_vol_bps = self._ob_ws.candle.volatility_bps
        ds.window_start = market.window_start
        ds.window_end = market.window_end
        ds.seconds_to_close = market.seconds_to_close
        ds.phase = phase
        ds.yes_order_active = state.yes.has_order
        ds.no_order_active = state.no.has_order
        ds.yes_order_price = state.yes.order.price if state.yes.order else 0.0
        ds.no_order_price = state.no.order.price if state.no.order else 0.0
        ds.total_trades = self._stats.total_trades
        ds.wins = self._stats._wins
        ds.losses = self._stats._losses
        ds.total_pnl = self._stats.total_pnl
        ds.win_rate = self._stats.win_rate or 0.0
        ds.rolling_win_rate = self._stats.rolling_win_rate() or 0.0
        # Paper balance
        if hasattr(self._client, 'balance'):
            ds.paper_balance = self._client.balance
        ds.last_update = time.time()

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
                    max_entry_price=Config.MAX_ENTRY_PRICE,
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
        candle_vol = self._ob_ws.candle.volatility_bps
        if candle_vol > Config.VOLATILITY_GATE_BPS:
            log.info(
                "Vol gate SKIP | candle_vol=%.0f bps > gate=%.0f bps",
                candle_vol, Config.VOLATILITY_GATE_BPS,
            )
            await self._cancel_window(state)
            return

        # fair_prices() calls p_up_signal() internally — no duplicate call needed.
        # fair_prices returns (p_up, 1-p_up), so fair_yes == p_up by definition.
        fair_yes, fair_no = self._calc.fair_prices(market)
        p_up = fair_yes

        # --- Fetch real Polymarket best ask prices ----------------------
        # We buy outcome tokens, so the relevant price is the best ask.
        # Fetch both sides concurrently to minimise latency.
        yes_prices, no_prices = await asyncio.gather(
            self._client.get_best_prices(state.yes.token_id),
            self._client.get_best_prices(state.no.token_id),
        )
        yes_best_ask = yes_prices.get("best_ask")
        no_best_ask = no_prices.get("best_ask")

        # Update dashboard with market prices
        ds = dashboard_state
        ds.market_yes_ask = yes_best_ask
        ds.market_no_ask = no_best_ask

        tasks = []

        now = time.monotonic()
        if now - self._last_entry_log >= 2.0:
            self._last_entry_log = now
            log.info(
                "Entry window | BTC=%.2f  p_up=%.4f  fair_yes=%.4f  fair_no=%.4f  "
                "mkt_yes_ask=%s  mkt_no_ask=%s  vol=%.0fbps",
                self._ob_ws.book.mid_price or 0, p_up, fair_yes, fair_no,
                f"{yes_best_ask:.4f}" if yes_best_ask else "N/A",
                f"{no_best_ask:.4f}" if no_best_ask else "N/A",
                candle_vol,
            )

        # ----- YES side: buy UP token if edge + break-even check pass -----
        safety = Config.BREAKEVEN_SAFETY_BPS / 10_000
        if yes_best_ask is not None and p_up > 0.5:
            target = min(yes_best_ask, Config.MAX_ENTRY_PRICE)
            edge = (fair_yes - target) * 10_000
            # Break-even win rate = entry price; require p_up > target + safety
            breakeven_ok = p_up > target + safety
            if edge >= Config.MIN_EDGE_BPS and breakeven_ok:
                if not state.yes.was_ever_active:
                    state.yes.p_signal_at_entry = p_up
                    state.yes.last_entry_price  = target
                    state.yes.was_ever_active   = True
                tasks.append(self._refresh_side(state.yes, target))
            else:
                if state.yes.has_order:
                    tasks.append(self._cancel_side(state.yes))
                if edge < Config.MIN_EDGE_BPS and yes_best_ask is not None:
                    log.debug(
                        "YES: edge=%.0fbps < min=%d | ask=%.4f fair=%.4f",
                        edge, Config.MIN_EDGE_BPS, target, fair_yes,
                    )
                elif not breakeven_ok:
                    log.debug(
                        "YES: break-even fail | p_up=%.4f <= target+safety=%.4f",
                        p_up, target + safety,
                    )
        else:
            if state.yes.has_order:
                tasks.append(self._cancel_side(state.yes))

        # ----- NO side: buy DOWN token if edge + break-even check pass -----
        p_down = 1.0 - p_up
        if no_best_ask is not None and p_up < 0.5:
            target = min(no_best_ask, Config.MAX_ENTRY_PRICE)
            edge = (fair_no - target) * 10_000
            breakeven_ok = p_down > target + safety
            if edge >= Config.MIN_EDGE_BPS and breakeven_ok:
                if not state.no.was_ever_active:
                    state.no.p_signal_at_entry = p_up
                    state.no.last_entry_price  = target
                    state.no.was_ever_active   = True
                tasks.append(self._refresh_side(state.no, target))
            else:
                if state.no.has_order:
                    tasks.append(self._cancel_side(state.no))
                if edge < Config.MIN_EDGE_BPS and no_best_ask is not None:
                    log.debug(
                        "NO: edge=%.0fbps < min=%d | ask=%.4f fair=%.4f",
                        edge, Config.MIN_EDGE_BPS, target, fair_no,
                    )
                elif not breakeven_ok:
                    log.debug(
                        "NO: break-even fail | p_down=%.4f <= target+safety=%.4f",
                        p_down, target + safety,
                    )
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
            # Resolve paper trade balance
            if hasattr(self._client, 'resolve_trade'):
                self._client.resolve_trade(won, Config.ORDER_SIZE_USDC, side.last_entry_price)
            # Push to dashboard
            shares = Config.ORDER_SIZE_USDC / side.last_entry_price
            pnl = shares * (1.0 - side.last_entry_price) if won else -Config.ORDER_SIZE_USDC
            dashboard_state.recent_trades.append(TradeSnapshot(
                timestamp=time.time(),
                window_start=market.window_start,
                side=side.side_label,
                entry_price=side.last_entry_price,
                size_usdc=Config.ORDER_SIZE_USDC,
                p_signal=side.p_signal_at_entry,
                won=won,
                pnl=pnl,
            ))
            # Keep only last 50
            if len(dashboard_state.recent_trades) > 50:
                dashboard_state.recent_trades = dashboard_state.recent_trades[-50:]

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
            log.info("Cancelled %d order(s) for window %s", len(tasks), state.market.window_start)

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
