"""
Polymarket BTC 5-minute two-sided market maker.

Strategy
--------
For each 5-minute BTC window:

1. Compute fair YES / NO prices via time-adjusted random-walk CDF:
       p_up = Phi(ret / sigma_remaining)
   where sigma_remaining shrinks toward zero as the window closes.
2. Throughout the ENTIRE window (not just last 10 s):
   - BUY YES at (fair_yes - spread)
   - BUY NO  at (fair_no  - spread)
   Spread narrows as the window progresses (more certainty near close).
3. Ensure bid_yes + bid_no < 1.0 so that if BOTH sides fill,
   the combined cost < $1 and the bot profits regardless of outcome.
4. Every QUOTE_REFRESH_MS ms, recalculate fair prices and cancel/replace
   if price has drifted beyond threshold.
5. EXIT_WINDOW_SEC before close, cancel all orders.

Risk controls
-------------
- Volatility gate: skip quoting when 5m candle range > VOLATILITY_GATE_BPS
- Stale data guard: skip when Binance book age > STALE_DATA_MAX_SEC
- Circuit breaker: stop quoting if session P&L < -MAX_LOSS_USDC
- Paper fill simulation: orders only fill when Polymarket ask <= our bid

Orderbook awareness
-------------------
A background loop polls Polymarket CLOB best ask/bid every ORDERBOOK_POLL_SEC.
Used for dashboard display and (in paper mode) realistic fill simulation.
"""
import asyncio
import logging
import time
from typing import Optional

from bot_state import state as dashboard_state, TradeSnapshot
from config import Config
from market_calculator import BtcMarket, MarketCalculator
from polymarket_client import MakerOrder, PolymarketClient, SIDE_BUY
from stats import BotStats
from ws_orderbook import OrderBookWS

log = logging.getLogger(__name__)

# Minimum price change to trigger a cancel/replace.
# 1 cent — reduces churn vs the old 0.5-cent threshold while still
# tracking fair-price moves that matter for our 1.5-4.5 cent spread.
PRICE_DRIFT_THRESHOLD = 0.01


class MarketSide:
    """Holds the live maker order for one side (YES or NO) of one market."""

    def __init__(self, token_id: str, side_label: str):
        self.token_id   = token_id
        self.side_label = side_label  # "YES" or "NO"
        self.order: Optional[MakerOrder] = None

        # Stats fields — survive order cancellation so _evaluate_and_record_window
        # can read them at rollover time even after EXIT_WINDOW cancel.
        self.was_ever_active:    bool  = False   # True if an order was placed
        self.was_ever_filled:    bool  = False   # True if order filled (paper: market crossed)
        self.p_signal_at_entry:  float = 0.0     # p_up when first order was placed
        self.last_entry_price:   float = 0.0     # price of most recent order

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
    Two-sided market maker for Polymarket BTC 5-minute markets.

    Quotes BUY orders on BOTH YES and NO throughout each window.
    One WindowState is active at a time.
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
        self._last_status_log: float = 0.0
        self._last_quote_log: float = 0.0
        self._last_volgate_log: float = 0.0

        # CLOB orderbook cache (updated by _clob_poll_loop)
        self._last_yes_ask: Optional[float] = None
        self._last_no_ask:  Optional[float] = None

        # Circuit breaker state
        self._circuit_open = False

    @property
    def _is_paper(self) -> bool:
        return hasattr(self._client, 'resolve_trade')

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        log.info("MarketMaker starting (two-sided mode)")

        await self._client.check_approvals()
        await self._refresh_market_list()

        asyncio.create_task(self._market_refresh_loop(), name="market-refresh")
        asyncio.create_task(self._clob_poll_loop(), name="clob-poll")

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
        """Hot-swap the trading client (paper <-> live) without stopping."""
        log.info("Swapping client -> %s", type(new_client).__name__)
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
        self._stats.log_summary()
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

        # --- Stale data guard ---
        book_age = time.time() - self._ob_ws.book.last_update_ms / 1000
        if book_age > Config.STALE_DATA_MAX_SEC:
            now = time.monotonic()
            if now - self._last_status_log >= 30.0:
                self._last_status_log = now
                log.warning(
                    "Stale data SKIP | book age=%.1fs > limit=%.0fs",
                    book_age, Config.STALE_DATA_MAX_SEC,
                )
            return

        # --- Circuit breaker ---
        if self._check_circuit_breaker():
            return

        # --- Determine phase ---
        stc = market.seconds_to_close
        if stc <= Config.EXIT_WINDOW_SEC:
            phase = "exit"
        else:
            phase = "quoting"

        self._update_dashboard(market, state, phase)

        if phase == "exit":
            await self._cancel_window(state)
            return

        await self._quote_both_sides(state, market)

    def _check_circuit_breaker(self) -> bool:
        """Stop trading if session P&L drops below -MAX_LOSS_USDC."""
        if self._stats.total_pnl < -Config.MAX_LOSS_USDC:
            if not self._circuit_open:
                self._circuit_open = True
                log.warning(
                    "CIRCUIT BREAKER | session P&L=%.2f < -%.2f — quoting stopped",
                    self._stats.total_pnl, Config.MAX_LOSS_USDC,
                )
            return True
        if self._circuit_open:
            self._circuit_open = False
            log.info("Circuit breaker reset | P&L=%.2f", self._stats.total_pnl)
        return False

    def _update_dashboard(self, market: BtcMarket, state: WindowState, phase: str) -> None:
        """Push current state to the shared dashboard object."""
        mid = self._ob_ws.book.mid_price or 0.0
        p_up = self._calc.p_up_signal(market)
        spread = self._calc.dynamic_spread(market)

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
        ds.spread = spread
        ds.realized_sigma = self._ob_ws.realized_sigma_5m
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
        if hasattr(self._client, 'balance'):
            ds.paper_balance = self._client.balance
        ds.last_update = time.time()

    async def _rollover(self, new_market: BtcMarket) -> None:
        """Clean up old window, evaluate its outcome, set up new window state."""
        if self._state:
            self._evaluate_and_record_window(self._state)
            log.info("Window rolled over — cancelling old orders")
            await self._cancel_window(self._state)

            self._windows_since_stats_log += 1
            if self._windows_since_stats_log >= Config.STATS_LOG_INTERVAL:
                self._windows_since_stats_log = 0
                self._stats.log_summary()

        # Reset CLOB cache for new window
        self._last_yes_ask = None
        self._last_no_ask = None

        self._state = WindowState(new_market)
        log.info(
            "New window: %s -> %s",
            new_market.window_start,
            new_market.window_end,
        )

    # ------------------------------------------------------------------
    # Two-sided quoting (core strategy)
    # ------------------------------------------------------------------

    # Inventory skew: reduce spread on unfilled side to attract fills
    _INVENTORY_SKEW = 0.4  # 40% spread reduction on the unfilled hedge side

    async def _quote_both_sides(self, state: WindowState, market: BtcMarket) -> None:
        """
        Place or refresh maker BUY orders on both YES and NO sides.

        Pricing pipeline:
        1. Compute fair prices from signal model
        2. Compute dynamic spread (time-based)
        3. Apply inventory skew if one side already filled
        4. Adjust bids using real CLOB ask (orderbook-aware pricing)
        5. Enforce bid_yes + bid_no < 1.0 invariant

        If both fill, total cost < $1 → guaranteed profit.
        """
        # --- Volatility gate ---
        candle_vol = self._ob_ws.candle.volatility_bps
        if candle_vol > Config.VOLATILITY_GATE_BPS:
            now = time.monotonic()
            if now - self._last_volgate_log >= 5.0:
                self._last_volgate_log = now
                log.info(
                    "Vol gate SKIP | candle_vol=%.0f bps > gate=%.0f bps",
                    candle_vol, Config.VOLATILITY_GATE_BPS,
                )
            await self._cancel_window(state)
            return

        # --- Fair prices and base spread ---
        fair_yes, fair_no = self._calc.fair_prices(market)
        p_up = fair_yes
        base_spread = self._calc.dynamic_spread(market)
        min_spread_price = Config.MIN_SPREAD_BPS / 10_000

        # --- Inventory skew: tighten spread on unfilled hedge side ---
        yes_spread = base_spread
        no_spread = base_spread
        if state.yes.was_ever_filled and not state.no.was_ever_filled:
            no_spread *= (1.0 - self._INVENTORY_SKEW)
        elif state.no.was_ever_filled and not state.yes.was_ever_filled:
            yes_spread *= (1.0 - self._INVENTORY_SKEW)

        # --- Orderbook-aware bids ---
        yes_bid = self._calc.orderbook_aware_bid(
            fair=fair_yes, spread=yes_spread,
            market_ask=self._last_yes_ask,
            min_spread_price=min_spread_price,
        )
        no_bid = self._calc.orderbook_aware_bid(
            fair=fair_no, spread=no_spread,
            market_ask=self._last_no_ask,
            min_spread_price=min_spread_price,
        )

        # Cap at MAX_ENTRY_PRICE
        yes_bid = min(yes_bid, Config.MAX_ENTRY_PRICE)
        no_bid  = min(no_bid,  Config.MAX_ENTRY_PRICE)

        # Guarantee: bid_yes + bid_no < 1.0 (profit if both fill)
        total = yes_bid + no_bid
        if total >= 1.0:
            scale = 0.98 / total
            yes_bid = round(yes_bid * scale, 2)
            no_bid  = round(no_bid  * scale, 2)

        # --- Periodic log ---
        now = time.monotonic()
        if now - self._last_quote_log >= 5.0:
            self._last_quote_log = now
            sigma = self._ob_ws.realized_sigma_5m
            skew_label = ""
            if state.yes.was_ever_filled and not state.no.was_ever_filled:
                skew_label = " [skew→NO]"
            elif state.no.was_ever_filled and not state.yes.was_ever_filled:
                skew_label = " [skew→YES]"
            log.info(
                "Quoting | BTC=%.2f  p_up=%.4f  σ=%.4f  spread=%.4f  "
                "yes_bid=%.2f  no_bid=%.2f  sum=%.2f  "
                "mkt_ask_y=%s  mkt_ask_n=%s  vol=%.0fbps  stc=%.0fs%s",
                self._ob_ws.book.mid_price or 0, p_up, sigma, base_spread,
                yes_bid, no_bid, yes_bid + no_bid,
                f"{self._last_yes_ask:.2f}" if self._last_yes_ask else "?",
                f"{self._last_no_ask:.2f}" if self._last_no_ask else "?",
                candle_vol, market.seconds_to_close, skew_label,
            )

        # --- Place/refresh orders on both sides ---
        tasks = []

        # YES side
        if yes_bid >= 0.01:
            if not state.yes.was_ever_active:
                state.yes.p_signal_at_entry = p_up
                state.yes.was_ever_active = True
                if not self._is_paper:
                    state.yes.was_ever_filled = True  # live: assume fill
            state.yes.last_entry_price = yes_bid
            tasks.append(self._refresh_side(state.yes, yes_bid))

        # NO side
        if no_bid >= 0.01:
            if not state.no.was_ever_active:
                state.no.p_signal_at_entry = p_up
                state.no.was_ever_active = True
                if not self._is_paper:
                    state.no.was_ever_filled = True  # live: assume fill
            state.no.last_entry_price = no_bid
            tasks.append(self._refresh_side(state.no, no_bid))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # CLOB orderbook polling (background)
    # ------------------------------------------------------------------

    async def _clob_poll_loop(self) -> None:
        """
        Poll Polymarket CLOB for best bid/ask every ORDERBOOK_POLL_SEC.
        Updates dashboard and checks paper fills.
        """
        while self._running:
            await asyncio.sleep(Config.ORDERBOOK_POLL_SEC)
            if not self._running or self._state is None:
                continue
            state = self._state
            try:
                yes_p, no_p = await asyncio.gather(
                    self._client.get_best_prices(state.yes.token_id),
                    self._client.get_best_prices(state.no.token_id),
                )
                self._last_yes_ask = yes_p.get("best_ask")
                self._last_no_ask  = no_p.get("best_ask")

                # Update dashboard
                dashboard_state.market_yes_ask = self._last_yes_ask
                dashboard_state.market_no_ask  = self._last_no_ask

                # Paper fill simulation
                if self._is_paper:
                    self._check_paper_fills(state)

            except Exception as exc:
                log.debug("CLOB poll error: %s", exc)

    def _check_paper_fills(self, state: WindowState) -> None:
        """
        In paper mode, check if our resting orders would have filled.
        A BUY order fills if the Polymarket market ask <= our bid price.
        """
        for side, ask in [
            (state.yes, self._last_yes_ask),
            (state.no,  self._last_no_ask),
        ]:
            if side.was_ever_filled or not side.has_order or ask is None:
                continue
            if side.order.price >= ask:
                side.was_ever_filled = True
                log.info(
                    "Paper FILL | %s @ %.4f (market ask=%.4f)",
                    side.side_label, side.order.price, ask,
                )

    # ------------------------------------------------------------------
    # Window evaluation
    # ------------------------------------------------------------------

    def _evaluate_and_record_window(self, state: WindowState) -> None:
        """
        Approximate trade outcome for the closing window and record it.

        Only resolves sides where was_ever_filled is True:
        - Live mode: filled = True when order placed (optimistic)
        - Paper mode: filled = True only when CLOB ask <= our bid
        """
        market = state.market
        mid    = self._ob_ws.book.mid_price
        if mid is None or market.open_price is None:
            return

        btc_closed_up = mid >= market.open_price
        both_filled = state.yes.was_ever_filled and state.no.was_ever_filled

        for side in (state.yes, state.no):
            if not side.was_ever_filled:
                continue
            signal_is_up = (side.side_label == "YES")
            won = (btc_closed_up == signal_is_up)
            entry_price = side.last_entry_price if side.last_entry_price > 0 else 0.01

            self._stats.record_trade(
                window_start=market.window_start,
                side=side.side_label,
                entry_price=entry_price,
                size_usdc=Config.ORDER_SIZE_USDC,
                p_signal=side.p_signal_at_entry,
                won=won,
            )
            if hasattr(self._client, 'resolve_trade'):
                self._client.resolve_trade(won, Config.ORDER_SIZE_USDC, entry_price)

            shares = Config.ORDER_SIZE_USDC / entry_price
            pnl = shares * (1.0 - entry_price) if won else -Config.ORDER_SIZE_USDC
            dashboard_state.recent_trades.append(TradeSnapshot(
                timestamp=time.time(),
                window_start=market.window_start,
                side=side.side_label,
                entry_price=entry_price,
                size_usdc=Config.ORDER_SIZE_USDC,
                p_signal=side.p_signal_at_entry,
                won=won,
                pnl=pnl,
            ))
            if len(dashboard_state.recent_trades) > 50:
                dashboard_state.recent_trades = dashboard_state.recent_trades[-50:]

        # Log summary for windows where at least one side was active
        if state.yes.was_ever_active or state.no.was_ever_active:
            yes_fill = "FILL" if state.yes.was_ever_filled else "no-fill"
            no_fill  = "FILL" if state.no.was_ever_filled else "no-fill"
            if both_filled:
                yes_p = state.yes.last_entry_price or 0
                no_p  = state.no.last_entry_price or 0
                margin_cents = (1.0 - yes_p - no_p) * 100
                log.info(
                    "Two-sided | yes@%.2f(%s) + no@%.2f(%s) = %.2f | margin=%.1f¢ | %s",
                    yes_p, yes_fill, no_p, no_fill, yes_p + no_p,
                    margin_cents, "UP" if btc_closed_up else "DOWN",
                )
            else:
                log.info(
                    "One-sided | yes(%s) no(%s) | %s",
                    yes_fill, no_fill, "UP" if btc_closed_up else "DOWN",
                )

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    async def _refresh_side(self, side: MarketSide, target_price: float) -> None:
        """Place a new order or cancel/replace if price drifted."""
        if not side.has_order:
            try:
                order = await self._client.place_maker_order(
                    token_id=side.token_id,
                    side=SIDE_BUY,
                    price=target_price,
                    size_usdc=Config.ORDER_SIZE_USDC,
                )
                side.order = order
            except Exception as exc:
                log.error("place_maker_order %s failed: %s", side.side_label, exc)
        elif side.price_drifted(target_price):
            t0 = time.monotonic()
            new_order = await self._client.cancel_replace(
                old_order=side.order,
                new_price=target_price,
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
            log.debug(
                "cancel_replace %s %.4f->%.4f in %.1f ms",
                side.side_label, side.order.price, target_price, elapsed_ms,
            )
            side.order = new_order

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
    # Background loops
    # ------------------------------------------------------------------

    async def _market_refresh_loop(self) -> None:
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
