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
from market_calculator import BtcMarket, MarketCalculator, SignalResult, compute_fee_per_share, compute_fee
from polymarket_client import MakerOrder, PolymarketClient, SIDE_BUY, SIDE_SELL
from stats import BotStats
from trade_logger import TradeLogger, TradeRow
from ws_orderbook import OrderBookWS

log = logging.getLogger(__name__)

# Minimum price change to trigger a cancel/replace.
# 2 cents — reduces churn from constant BTC oscillations while still
# tracking meaningful fair-price moves within our 1.5-4.5 cent spread.
PRICE_DRIFT_THRESHOLD = 0.02


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
        self.first_fill_time:    float = 0.0     # monotonic time of first fill (for hedge timeout)
        self.p_signal_at_entry:  float = 0.0     # p_up when first order was placed
        self.last_entry_price:   float = 0.0     # price of most recent order
        self.last_entry_size:    float = 0.0     # USDC size of most recent order

        # Sell-side exit: order to sell filled tokens during hedge timeout
        self.sell_order: Optional[MakerOrder] = None
        # Stop-loss: True if position was exited early via stop-loss
        self.stopped_out:        bool  = False
        self.stop_loss_pnl:      float = 0.0     # P&L from stop-loss exit

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
        self.stopped_out: bool = False  # True if stop-loss triggered this window

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
        self._logger  = TradeLogger()
        self._windows_since_stats_log = 0
        self._last_status_log: float = 0.0
        self._last_quote_log: float = 0.0
        self._last_volgate_log: float = 0.0

        # CLOB orderbook cache (updated by _clob_poll_loop)
        self._last_yes_ask: Optional[float] = None
        self._last_no_ask:  Optional[float] = None
        self._last_yes_bid: Optional[float] = None
        self._last_no_bid:  Optional[float] = None

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

        # Write session summary CSV
        from collections import Counter
        window_sides: Counter = Counter()
        for t in self._stats._trades:
            window_sides[t.window_start] += 1
        two_sided = sum(1 for c in window_sides.values() if c >= 2)
        balance = self._client.balance if hasattr(self._client, 'balance') else 0.0
        self._logger.log_session_summary(
            total_trades=self._stats.total_trades,
            wins=self._stats._wins,
            losses=self._stats._losses,
            stop_losses=dashboard_state.stop_losses,
            total_pnl=self._stats.total_pnl,
            total_fees=self._stats._total_fees,
            final_balance=balance,
            avg_entry_price=self._stats.avg_entry_price or 0.0,
            two_sided_fills=two_sided,
        )

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
            self._update_dashboard(market, state, "exit")
            await self._cancel_window(state)
            return

        # _quote_both_sides updates dashboard with accurate phase
        await self._quote_both_sides(state, market)

    def _check_circuit_breaker(self) -> bool:
        """Stop trading if drawdown from peak exceeds MAX_LOSS_USDC."""
        drawdown = abs(self._stats.current_drawdown)
        if drawdown > Config.MAX_LOSS_USDC:
            if not self._circuit_open:
                self._circuit_open = True
                log.warning(
                    "CIRCUIT BREAKER | drawdown=%.2f > limit=%.2f | P&L=%.2f — quoting stopped",
                    drawdown, Config.MAX_LOSS_USDC, self._stats.total_pnl,
                )
            return True
        if self._circuit_open:
            self._circuit_open = False
            log.info("Circuit breaker reset | drawdown=%.2f | P&L=%.2f",
                     drawdown, self._stats.total_pnl)
        return False

    def _update_dashboard(self, market: BtcMarket, state: WindowState,
                          phase: str, signal: Optional[SignalResult] = None) -> None:
        """Push current state to the shared dashboard object."""
        mid = self._ob_ws.book.mid_price or 0.0
        if signal is None:
            signal = self._calc.compute_signal(market)
        p_up = signal.p_up
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
        ds.hourly_trend_bias = self._ob_ws.hourly_trend_bias
        ds.obi = self._ob_ws.smoothed_obi
        ds.vol_regime = self._ob_ws.vol_regime
        ds.volume_ratio = self._ob_ws.volume_ratio

        # Signal quality fields
        ds.signal_confidence = signal.confidence
        ds.signal_raw_p_up = signal.raw_p_up
        ds.signal_factors = signal.factors

        # Tick momentum
        ds.tick_momentum = getattr(self._ob_ws, 'tick_momentum', 0.0)

        # EV per side
        yes_bid = state.yes.order.price if state.yes.order else (p_up - spread)
        no_bid = state.no.order.price if state.no.order else ((1.0 - p_up) - spread)
        ds.yes_ev = self._calc.expected_value(p_up, max(0.05, yes_bid), Config.ORDER_SIZE_USDC)
        ds.no_ev = self._calc.expected_value(1.0 - p_up, max(0.05, no_bid), Config.ORDER_SIZE_USDC)

        # Advanced stats
        ds.sharpe_ratio = self._stats.sharpe_ratio
        ds.max_drawdown = self._stats.max_drawdown
        ds.profit_factor = self._stats.profit_factor
        ds.consecutive_losses = self._stats.consecutive_losses
        ds.max_win_streak = self._stats._max_win_streak
        ds.max_loss_streak = self._stats._max_loss_streak

        # Hedge timeout status
        hedge_active = False
        now_mono = time.monotonic()
        if state.yes.was_ever_filled and not state.no.was_ever_filled:
            if state.yes.first_fill_time > 0 and (now_mono - state.yes.first_fill_time) > Config.HEDGE_TIMEOUT_SEC:
                hedge_active = True
        elif state.no.was_ever_filled and not state.yes.was_ever_filled:
            if state.no.first_fill_time > 0 and (now_mono - state.no.first_fill_time) > Config.HEDGE_TIMEOUT_SEC:
                hedge_active = True
        ds.hedge_timeout_active = hedge_active

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
        self._last_yes_bid = None
        self._last_no_bid = None

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
        1. Time-weighted entry: skip if in quiet period or signal too weak
        2. Compute fair prices from full signal model (with confidence)
        3. Gate on confidence and EV thresholds
        4. Compute dynamic spread + fee adjustment (both modes)
        5. Apply inventory skew if one side already filled
        6. Adjust bids using real CLOB ask (orderbook-aware pricing)
        7. Compute Kelly-optimal order sizes with drawdown/streak reduction
        8. Enforce bid_yes + bid_no < 1.0 invariant
        9. Enforce per-window loss cap

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
            self._update_dashboard(market, state, "vol_skip")
            return

        stc = market.seconds_to_close
        elapsed = Config.MARKET_WINDOW_SEC - stc

        # --- Window stopped out by stop-loss — no further quoting ---
        if state.stopped_out:
            self._update_dashboard(market, state, "stopped")
            return

        # --- Time-weighted entry: skip quiet period early in window ---
        if elapsed < Config.QUIET_PERIOD_SEC:
            self._update_dashboard(market, state, "waiting")
            return

        # --- Full signal computation (with confidence and factors) ---
        signal = self._calc.compute_signal(market)
        p_up = signal.p_up
        fair_yes = p_up
        fair_no = 1.0 - p_up

        # --- Confidence gate ---
        if signal.confidence < Config.MIN_CONFIDENCE:
            if state.yes.has_order or state.no.has_order:
                await self._cancel_window(state)
            self._update_dashboard(market, state, "low_conf", signal)
            return

        # --- Adaptive stop-loss: threshold = f(σ, stc) ---
        if Config.STOP_LOSS_ENABLED and stc > Config.STOP_LOSS_MIN_STC:
            sigma = self._ob_ws.realized_sigma_5m
            base_sl = Config.STOP_LOSS_BASE
            vol_adj = Config.STOP_LOSS_VOL_SCALE * (sigma / 0.002)
            time_factor = max(0.5, min(1.0, stc / 120.0))
            threshold = (base_sl + vol_adj) * time_factor

            for side in (state.yes, state.no):
                if not side.was_ever_filled or side.stopped_out:
                    continue
                current_fair = p_up if side.side_label == "YES" else 1.0 - p_up
                reversal = side.last_entry_price - current_fair
                if reversal > threshold:
                    await self._stop_loss_exit(state, side, current_fair)
                    return

        # --- Minimum signal edge filter ---
        if abs(p_up - 0.5) < Config.MIN_SIGNAL_EDGE:
            if state.yes.has_order or state.no.has_order:
                await self._cancel_window(state)
            self._update_dashboard(market, state, "weak_signal", signal)
            return

        base_spread = self._calc.dynamic_spread(market)

        # --- Volatility regime adjustment ---
        vol_regime = self._ob_ws.vol_regime
        if vol_regime == "storm":
            base_spread *= Config.VOL_REGIME_STORM_SPREAD_MULT
        elif vol_regime == "calm":
            base_spread *= Config.VOL_REGIME_CALM_SPREAD_MULT

        min_spread_price = Config.MIN_SPREAD_BPS / 10_000

        # --- Inventory skew + smart hedge timeout ---
        yes_spread = base_spread
        no_spread = base_spread
        now_mono = time.monotonic()

        # Dynamic timeout: fraction of remaining time, capped by HEDGE_TIMEOUT_SEC
        dynamic_timeout = max(5.0, min(Config.HEDGE_TIMEOUT_SEC, stc * Config.HEDGE_TIMEOUT_FRAC))

        def _should_aggressive_hedge(filled_side: MarketSide) -> bool:
            if not Config.HEDGE_ONLY_IF_LOSING:
                return True
            current_fair = p_up if filled_side.side_label == "YES" else 1.0 - p_up
            return filled_side.last_entry_price > current_fair + 0.02

        if state.yes.was_ever_filled and not state.no.was_ever_filled:
            elapsed_since_fill = now_mono - state.yes.first_fill_time if state.yes.first_fill_time > 0 else 0.0
            if elapsed_since_fill > dynamic_timeout and _should_aggressive_hedge(state.yes):
                no_spread *= Config.HEDGE_AGGRESSIVE_SPREAD_MULT
                if Config.SELL_EXIT_ENABLED:
                    await self._try_sell_exit(state.yes, self._last_yes_bid)
            else:
                no_spread *= (1.0 - self._INVENTORY_SKEW)
        elif state.no.was_ever_filled and not state.yes.was_ever_filled:
            elapsed_since_fill = now_mono - state.no.first_fill_time if state.no.first_fill_time > 0 else 0.0
            if elapsed_since_fill > dynamic_timeout and _should_aggressive_hedge(state.no):
                yes_spread *= Config.HEDGE_AGGRESSIVE_SPREAD_MULT
                if Config.SELL_EXIT_ENABLED:
                    await self._try_sell_exit(state.no, self._last_no_bid)
            else:
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

        # --- Fee-aware spread: widen bids to cover exact Polymarket fee ---
        # Applied in BOTH paper and live mode for consistent simulation.
        yes_fee_adj = compute_fee_per_share(yes_bid)
        no_fee_adj  = compute_fee_per_share(no_bid)
        yes_bid = round(yes_bid - yes_fee_adj, 2)
        no_bid  = round(no_bid  - no_fee_adj, 2)

        # Cap at MAX_ENTRY_PRICE
        yes_bid = min(yes_bid, Config.MAX_ENTRY_PRICE)
        no_bid  = min(no_bid,  Config.MAX_ENTRY_PRICE)

        # Guarantee: bid_yes + bid_no + fees < 1.0 (profit if both fill).
        # The old check ignored fees, so two-sided fills could be net-negative.
        yes_shares = Config.ORDER_SIZE_USDC / yes_bid if yes_bid > 0 else 0
        no_shares  = Config.ORDER_SIZE_USDC / no_bid  if no_bid  > 0 else 0
        fee_yes = compute_fee(yes_shares, yes_bid) / yes_shares if yes_shares > 0 else 0
        fee_no  = compute_fee(no_shares,  no_bid)  / no_shares  if no_shares  > 0 else 0
        total_cost = yes_bid + no_bid + fee_yes + fee_no
        if total_cost >= 1.0:
            scale = 0.98 / total_cost
            yes_bid = round(yes_bid * scale, 2)
            no_bid  = round(no_bid  * scale, 2)

        # --- EV filter: reject trades with marginal expected value ---
        yes_ev = self._calc.expected_value(p_up, yes_bid, Config.ORDER_SIZE_USDC)
        no_ev = self._calc.expected_value(1.0 - p_up, no_bid, Config.ORDER_SIZE_USDC)

        # --- Kelly sizing (confidence-adjusted) ---
        yes_size = self._calc.kelly_size(p_up, yes_bid, Config.ORDER_SIZE_USDC, signal.confidence)
        no_size  = self._calc.kelly_size(1.0 - p_up, no_bid, Config.ORDER_SIZE_USDC, signal.confidence)

        # --- Vol regime size reduction ---
        if vol_regime == "storm":
            yes_size *= Config.VOL_REGIME_STORM_SIZE_MULT
            no_size *= Config.VOL_REGIME_STORM_SIZE_MULT

        # --- Drawdown-based size reduction ---
        if Config.DRAWDOWN_SIZE_REDUCTION:
            dd = abs(self._stats.current_drawdown)
            if dd > 0:
                dd_mult = max(
                    Config.DRAWDOWN_MIN_SIZE_MULT,
                    1.0 - 0.5 * (dd / Config.DRAWDOWN_FULL_REDUCE_USDC),
                )
                yes_size *= dd_mult
                no_size *= dd_mult

        # --- Consecutive loss size reduction ---
        if self._stats.consecutive_losses >= Config.CONSEC_LOSS_REDUCE_AFTER:
            yes_size *= Config.CONSEC_LOSS_SIZE_MULT
            no_size *= Config.CONSEC_LOSS_SIZE_MULT

        # --- Per-window loss cap ---
        window_exposure = 0.0
        if state.yes.was_ever_filled:
            window_exposure += state.yes.last_entry_size
        if state.no.was_ever_filled:
            window_exposure += state.no.last_entry_size
        remaining_budget = max(0.0, Config.MAX_LOSS_PER_WINDOW_USDC - window_exposure)
        yes_size = min(yes_size, remaining_budget)
        no_size = min(no_size, remaining_budget)

        # --- Periodic log ---
        now = time.monotonic()
        if now - self._last_quote_log >= 5.0:
            self._last_quote_log = now
            sigma = self._ob_ws.realized_sigma_5m
            skew_label = ""
            if state.yes.was_ever_filled and not state.no.was_ever_filled:
                elapsed_f = now - state.yes.first_fill_time if state.yes.first_fill_time > 0 else 0.0
                if elapsed_f > dynamic_timeout:
                    skew_label = " [HEDGE-RUSH→NO %.0fs]" % elapsed_f
                else:
                    skew_label = " [skew→NO]"
            elif state.no.was_ever_filled and not state.yes.was_ever_filled:
                elapsed_f = now - state.no.first_fill_time if state.no.first_fill_time > 0 else 0.0
                if elapsed_f > dynamic_timeout:
                    skew_label = " [HEDGE-RUSH→YES %.0fs]" % elapsed_f
                else:
                    skew_label = " [skew→YES]"
            log.info(
                "Quoting | BTC=%.2f  p_up=%.4f  conf=%.2f  σ=%.4f  spread=%.4f  "
                "yes=%.2f($%.0f ev=%.2f)  no=%.2f($%.0f ev=%.2f)  sum=%.2f  "
                "mkt_ask_y=%s  mkt_ask_n=%s  vol=%.0fbps  stc=%.0fs  regime=%s%s",
                self._ob_ws.book.mid_price or 0, p_up, signal.confidence, sigma, base_spread,
                yes_bid, yes_size, yes_ev, no_bid, no_size, no_ev, yes_bid + no_bid,
                f"{self._last_yes_ask:.2f}" if self._last_yes_ask else "?",
                f"{self._last_no_ask:.2f}" if self._last_no_ask else "?",
                candle_vol, stc, vol_regime, skew_label,
            )

        # --- Signal direction filter ---
        HEDGE_BAND = 0.10
        place_yes = p_up >= (0.5 - HEDGE_BAND)
        place_no  = p_up <= (0.5 + HEDGE_BAND)

        # --- EV gate: don't place if EV below threshold ---
        if yes_ev < Config.MIN_EV_USDC:
            place_yes = False
        if no_ev < Config.MIN_EV_USDC:
            place_no = False

        min_bid = Config.MIN_BID_PRICE

        # --- Place/refresh orders on both sides ---
        tasks = []

        # YES side
        if place_yes and yes_bid >= min_bid and yes_size > 0:
            if state.yes.was_ever_filled:
                # Already filled — cancel resting order, stop refreshing
                if state.yes.has_order:
                    tasks.append(self._cancel_side(state.yes))
            else:
                if not state.yes.was_ever_active:
                    state.yes.p_signal_at_entry = p_up
                    state.yes.was_ever_active = True
                state.yes.last_entry_price = yes_bid
                state.yes.last_entry_size = yes_size
                tasks.append(self._refresh_side(state.yes, yes_bid, yes_size))
        elif state.yes.has_order and not place_yes:
            # Cancel the wrong-side order if it was placed before signal shifted
            tasks.append(self._cancel_side(state.yes))

        # NO side
        if place_no and no_bid >= min_bid and no_size > 0:
            if state.no.was_ever_filled:
                # Already filled — cancel resting order, stop refreshing
                if state.no.has_order:
                    tasks.append(self._cancel_side(state.no))
            else:
                if not state.no.was_ever_active:
                    state.no.p_signal_at_entry = p_up
                    state.no.was_ever_active = True
                state.no.last_entry_price = no_bid
                state.no.last_entry_size = no_size
                tasks.append(self._refresh_side(state.no, no_bid, no_size))
        elif state.no.has_order and not place_no:
            tasks.append(self._cancel_side(state.no))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._update_dashboard(market, state, "quoting", signal)

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
                self._last_yes_bid = yes_p.get("best_bid")
                self._last_no_bid  = no_p.get("best_bid")

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
        In paper mode, use PaperClient.check_fill() for realistic fill simulation
        with slippage, latency, and partial fills.
        """
        for side, ask in [
            (state.yes, self._last_yes_ask),
            (state.no,  self._last_no_ask),
        ]:
            if side.was_ever_filled or not side.has_order or ask is None:
                continue
            fill_info = self._client.check_fill(side.order, ask)
            if fill_info is not None:
                side.was_ever_filled = True
                side.first_fill_time = time.monotonic()
                side.last_entry_price = fill_info["fill_price"]
                side.last_entry_size = fill_info["fill_size_usdc"]
                log.info(
                    "Paper FILL | %s @ %.4f (bid=%.4f, ask=%.4f) | "
                    "slip=%.1fbps lat=%.0fms fill=%.0f%%",
                    side.side_label, fill_info["fill_price"],
                    side.order.price, ask,
                    fill_info["slippage_bps"], fill_info["latency_ms"],
                    fill_info["fill_fraction"] * 100,
                )

    # ------------------------------------------------------------------
    # Stop-loss & sell-side exit
    # ------------------------------------------------------------------

    async def _stop_loss_exit(self, state: WindowState, side: MarketSide, current_fair: float) -> None:
        """
        Exit a filled position early when the signal reverses beyond threshold.

        Instead of waiting for binary resolution (win=+profit, loss=-size),
        we approximate P&L as if we sold at current fair value:
            pnl = shares × (current_fair - entry_price)
        This caps the loss and avoids full binary wipeout.
        """
        entry_price = side.last_entry_price
        size_usdc = side.last_entry_size
        shares = size_usdc / entry_price
        fee = compute_fee(shares, entry_price)
        pnl = shares * (current_fair - entry_price) - fee

        log.warning(
            "STOP-LOSS | %s filled@%.2f → fair=%.2f | reversal=%.2f | "
            "shares=%.1f | fee=%.4f | P&L=%.2f USDC",
            side.side_label, entry_price, current_fair,
            entry_price - current_fair, shares, fee, pnl,
        )

        # Cancel all orders for this window
        await self._cancel_window(state)

        # Record as a loss with custom P&L
        self._stats.record_trade(
            window_start=state.market.window_start,
            side=side.side_label,
            entry_price=entry_price,
            size_usdc=size_usdc,
            p_signal=side.p_signal_at_entry,
            won=False,
            pnl_override=pnl,
            fee=fee,
        )

        # Paper mode: adjust balance
        if hasattr(self._client, 'resolve_trade'):
            self._client.balance += pnl
            self._client._total_pnl += pnl
            self._client._trade_count += 1

        # Update dashboard
        dashboard_state.recent_trades.append(TradeSnapshot(
            timestamp=time.time(),
            window_start=state.market.window_start,
            side=side.side_label,
            entry_price=entry_price,
            size_usdc=size_usdc,
            p_signal=side.p_signal_at_entry,
            won=False,
            pnl=pnl,
            exit_type="stop-loss",
        ))
        if len(dashboard_state.recent_trades) > 50:
            dashboard_state.recent_trades = dashboard_state.recent_trades[-50:]

        # CSV log
        balance = self._client.balance if hasattr(self._client, 'balance') else 0.0
        self._logger.log_trade(TradeRow(
            window_start=state.market.window_start,
            window_end=state.market.window_end,
            side=side.side_label,
            entry_price=entry_price,
            size_usdc=size_usdc,
            p_signal=side.p_signal_at_entry,
            sigma=self._ob_ws.realized_sigma_5m,
            vol_regime=self._ob_ws.vol_regime,
            obi=self._ob_ws.smoothed_obi,
            volume_ratio=self._ob_ws.volume_ratio,
            spread=self._calc.dynamic_spread(state.market),
            btc_open=state.market.open_price or 0.0,
            btc_close=self._ob_ws.book.mid_price or 0.0,
            market_ask=self._last_yes_ask if side.side_label == "YES" else self._last_no_ask or 0.0,
            market_bid=self._last_yes_bid if side.side_label == "YES" else self._last_no_bid or 0.0,
            outcome="STOP-LOSS",
            pnl=pnl,
            fee=0.0,
            balance_after=balance,
            exit_type="stop-loss",
            stc_at_fill=Config.MARKET_WINDOW_SEC - (side.first_fill_time - state.market.window_start) if side.first_fill_time > 0 else 0.0,
        ))

        # Mark as stopped out to prevent double evaluation at rollover
        side.stopped_out = True
        side.stop_loss_pnl = pnl
        side.was_ever_filled = False  # prevent _evaluate_and_record_window from re-counting
        state.stopped_out = True
        dashboard_state.stop_losses += 1

    async def _try_sell_exit(self, filled_side: MarketSide, market_bid: Optional[float]) -> None:
        """
        Place a SELL order on the filled token to exit the position.

        Called during hedge timeout when one side is filled but the opposite
        hasn't. Selling the filled token caps losses instead of relying on
        the opposite BUY to fill.
        """
        if filled_side.sell_order is not None:
            return  # already placed a sell order
        if filled_side.stopped_out:
            return
        if market_bid is None or market_bid <= 0.01:
            return

        entry_price = filled_side.last_entry_price
        size_usdc = filled_side.last_entry_size
        if entry_price <= 0 or size_usdc <= 0:
            return

        # Calculate shares held and USDC received from selling
        shares = size_usdc / entry_price
        sell_price = market_bid
        sell_size_usdc = round(shares * sell_price, 2)

        if sell_size_usdc < 1.0:
            return  # too small to bother

        try:
            order = await self._client.place_maker_order(
                token_id=filled_side.token_id,
                side=SIDE_SELL,
                price=sell_price,
                size_usdc=sell_size_usdc,
            )
            if order:
                filled_side.sell_order = order
                log.info(
                    "SELL EXIT | %s @ %.4f (entry was %.4f) | "
                    "shares=%.1f | usdc_out=%.2f",
                    filled_side.side_label, sell_price, entry_price,
                    shares, sell_size_usdc,
                )
        except Exception as exc:
            log.error("sell_exit %s failed: %s", filled_side.side_label, exc)

    # ------------------------------------------------------------------
    # Window evaluation
    # ------------------------------------------------------------------

    def _evaluate_and_record_window(self, state: WindowState) -> None:
        """
        Approximate trade outcome for the closing window and record it.

        Only resolves sides where was_ever_filled is True:
        - Live mode: filled = True when CLOB confirms
        - Paper mode: filled = True only when CLOB ask <= our bid
        """
        market = state.market
        mid    = self._ob_ws.book.mid_price
        if mid is None or market.open_price is None:
            return

        btc_closed_up = mid >= market.open_price
        both_filled = state.yes.was_ever_filled and state.no.was_ever_filled

        for side in (state.yes, state.no):
            if not side.was_ever_filled or side.stopped_out:
                continue
            signal_is_up = (side.side_label == "YES")
            won = (btc_closed_up == signal_is_up)
            entry_price = side.last_entry_price if side.last_entry_price > 0 else 0.01
            size_usdc = side.last_entry_size if side.last_entry_size > 0 else Config.ORDER_SIZE_USDC

            shares = size_usdc / entry_price
            fee = compute_fee(shares, entry_price)

            # Compute signal confidence at evaluation time (for record)
            sig = self._calc.compute_signal(market)
            conf = sig.confidence

            self._stats.record_trade(
                window_start=market.window_start,
                side=side.side_label,
                entry_price=entry_price,
                size_usdc=size_usdc,
                p_signal=side.p_signal_at_entry,
                won=won,
                fee=fee,
                confidence=conf,
            )
            if hasattr(self._client, 'resolve_trade'):
                self._client.resolve_trade(won, size_usdc, entry_price)

            pnl = shares * (1.0 - entry_price) - fee if won else -size_usdc

            # CSV log
            balance = self._client.balance if hasattr(self._client, 'balance') else 0.0
            opp_side = state.no if side.side_label == "YES" else state.yes
            self._logger.log_trade(TradeRow(
                window_start=market.window_start,
                window_end=market.window_end,
                side=side.side_label,
                entry_price=entry_price,
                size_usdc=size_usdc,
                p_signal=side.p_signal_at_entry,
                sigma=self._ob_ws.realized_sigma_5m,
                vol_regime=self._ob_ws.vol_regime,
                obi=round(self._ob_ws.smoothed_obi, 4),
                volume_ratio=round(self._ob_ws.volume_ratio, 2),
                spread=round(self._calc.dynamic_spread(market), 4),
                btc_open=market.open_price or 0.0,
                btc_close=mid,
                market_ask=(self._last_yes_ask or 0.0) if side.side_label == "YES" else (self._last_no_ask or 0.0),
                market_bid=(self._last_yes_bid or 0.0) if side.side_label == "YES" else (self._last_no_bid or 0.0),
                outcome="WIN" if won else "LOSS",
                pnl=round(pnl, 4),
                fee=round(fee, 4),
                balance_after=round(balance, 2),
                exit_type="binary",
                hedge_filled=opp_side.was_ever_filled,
                hedge_side=opp_side.side_label if opp_side.was_ever_filled else "",
                hedge_price=opp_side.last_entry_price if opp_side.was_ever_filled else 0.0,
            ))

            dashboard_state.recent_trades.append(TradeSnapshot(
                timestamp=time.time(),
                window_start=market.window_start,
                side=side.side_label,
                entry_price=entry_price,
                size_usdc=size_usdc,
                p_signal=side.p_signal_at_entry,
                won=won,
                pnl=pnl,
                confidence=conf,
                exit_type="binary",
            ))
            if len(dashboard_state.recent_trades) > 50:
                dashboard_state.recent_trades = dashboard_state.recent_trades[-50:]

        # Log summary for windows where at least one side was active
        if state.yes.was_ever_active or state.no.was_ever_active or state.stopped_out:
            yes_fill = "STOP" if state.yes.stopped_out else ("FILL" if state.yes.was_ever_filled else "no-fill")
            no_fill  = "STOP" if state.no.stopped_out else ("FILL" if state.no.was_ever_filled else "no-fill")
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

    async def _refresh_side(
        self, side: MarketSide, target_price: float, size_usdc: float = 0.0,
    ) -> None:
        """Place a new order or cancel/replace if price drifted."""
        size = size_usdc or Config.ORDER_SIZE_USDC
        if not side.has_order:
            try:
                order = await self._client.place_maker_order(
                    token_id=side.token_id,
                    side=SIDE_BUY,
                    price=target_price,
                    size_usdc=size,
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
        # Cancel sell-side exit orders too
        for side in (state.yes, state.no):
            if side.sell_order:
                tasks.append(self._client.cancel_order(side.sell_order.order_id))
                side.sell_order = None
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
        # Fast refresh on cold start (every 30s), then slow down to 5 min
        delay = 30.0
        while self._running:
            await asyncio.sleep(delay)
            if self._running:
                await self._refresh_market_list()
                delay = min(delay * 2, 300.0)  # ramp: 30→60→120→240→300s

    async def _refresh_market_list(self) -> None:
        markets = await self._calc.fetch_upcoming_markets()
        if not markets:
            log.warning("No BTC 5m markets found — will retry on next refresh cycle")
        else:
            log.debug("Market list refreshed: %d markets", len(markets))
