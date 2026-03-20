"""
Core market making logic for BTC/USDT on Binance.

Strategy
--------
1. Every QUOTE_REFRESH_MS milliseconds:
   a. Read best bid/ask from the live order book WS.
   b. Compute fair mid-price (inventory-adjusted).
   c. Compute spread = max(min_spread_bps, market_spread_fraction * volatility_bps).
   d. Set bid_price = mid * (1 - half_spread), ask_price = mid * (1 + half_spread).
   e. If existing orders are within PRICE_TOLERANCE_BPS of targets → do nothing.
   f. Otherwise → cancel/replace each stale order in a single REST call per side.

2. Every REBALANCE_INTERVAL_SEC (5 minutes):
   - Refresh 5m OHLCV-based volatility estimate from the kline WS data.
   - Recompute inventory skew and re-quote.

Order state machine
-------------------
Each side (BID / ASK) holds at most one live order tracked by its orderId.
On startup, any pre-existing open orders are cancelled to start clean.

Performance
-----------
cancel_replace_order() is a single POST → one network round-trip.
On a cloud VM in the same region as Binance (ap-northeast-1 for binance.com)
RTT is typically 2–8 ms, well below the 100 ms budget.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from binance_client import BinanceClient
from config import Config
from ws_orderbook import OrderBook, OrderBookWS

log = logging.getLogger(__name__)

PRICE_TOLERANCE_BPS = 2   # don't re-quote if price moved < 2 bps
INVENTORY_SKEW_FACTOR = 0.3  # bps of skew per 1% of max position used


@dataclass
class OrderState:
    side: str           # "BUY" or "SELL"
    order_id: Optional[int] = None
    price: float = 0.0
    quantity: float = 0.0
    placed_at: float = field(default_factory=time.monotonic)


class MarketMaker:
    def __init__(self):
        self.client = BinanceClient()
        self.ob_ws = OrderBookWS()
        self.bid = OrderState("BUY")
        self.ask = OrderState("SELL")
        self._inventory_btc: float = 0.0   # net BTC position (positive = long)
        self._last_rebalance: float = 0.0
        self._lot_size: float = Config.MIN_ORDER_SIZE_BTC
        self._tick_size: float = 0.01  # default; updated from exchangeInfo

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        log.info("Starting market maker on %s", Config.SYMBOL)
        await self._init()

        ws_task = asyncio.create_task(self.ob_ws.run(), name="orderbook-ws")
        quote_task = asyncio.create_task(self._quote_loop(), name="quote-loop")

        try:
            await asyncio.gather(ws_task, quote_task)
        except asyncio.CancelledError:
            log.info("MarketMaker cancelled – cleaning up")
        finally:
            await self._shutdown()
            ws_task.cancel()
            quote_task.cancel()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def _init(self) -> None:
        """Fetch exchange info, cancel stale orders, read initial inventory."""
        info = await self.client.get_exchange_info()
        self._parse_exchange_info(info)
        log.info("Exchange info loaded. lot_size=%.6f tick_size=%.2f", self._lot_size, self._tick_size)

        open_orders = await self.client.get_open_orders()
        if open_orders:
            log.info("Cancelling %d pre-existing orders", len(open_orders))
            await self.client.cancel_all_orders()

        account = await self.client.get_account()
        self._update_inventory(account)
        log.info("Initial BTC inventory: %.6f", self._inventory_btc)

    def _parse_exchange_info(self, info: dict) -> None:
        for sym in info.get("symbols", []):
            if sym["symbol"] == Config.SYMBOL:
                for f in sym.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        self._lot_size = float(f["minQty"])
                    elif f["filterType"] == "PRICE_FILTER":
                        self._tick_size = float(f["tickSize"])
                break

    def _update_inventory(self, account: dict) -> None:
        base_asset = Config.SYMBOL.replace("USDT", "")
        for bal in account.get("balances", []):
            if bal["asset"] == base_asset:
                self._inventory_btc = float(bal["free"]) + float(bal["locked"])
                return

    # ------------------------------------------------------------------
    # Quote loop
    # ------------------------------------------------------------------

    async def _quote_loop(self) -> None:
        interval = Config.QUOTE_REFRESH_MS / 1000
        while True:
            t0 = time.monotonic()
            try:
                await self._quote_once()
            except Exception as exc:
                log.error("quote_once error: %s", exc, exc_info=True)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _quote_once(self) -> None:
        book: OrderBook = self.ob_ws.book
        if book.mid_price is None:
            return  # book not yet populated

        mid = self._fair_mid(book)
        half_spread = self._half_spread_bps() / 10_000

        bid_target = self._round_price(mid * (1 - half_spread))
        ask_target = self._round_price(mid * (1 + half_spread))
        qty = self._round_qty(Config.ORDER_SIZE_QUOTE / mid)

        if qty < self._lot_size:
            log.warning("Computed qty %.6f < min lot size %.6f – skipping", qty, self._lot_size)
            return

        await asyncio.gather(
            self._refresh_side(self.bid, "BUY", bid_target, qty),
            self._refresh_side(self.ask, "SELL", ask_target, qty),
        )

    async def _refresh_side(
        self,
        state: OrderState,
        side: str,
        target_price: float,
        qty: float,
    ) -> None:
        """Cancel/replace the order if price has drifted beyond tolerance."""
        if state.order_id is not None:
            drift_bps = abs(target_price - state.price) / state.price * 10_000
            if drift_bps < PRICE_TOLERANCE_BPS:
                return  # current order is still good

            t0 = time.monotonic()
            cid = self._new_client_id(side)
            try:
                resp = await self.client.cancel_replace_order(
                    cancel_order_id=state.order_id,
                    side=side,
                    price=target_price,
                    quantity=qty,
                    client_order_id=cid,
                )
                elapsed_ms = (time.monotonic() - t0) * 1000
                log.debug("cancel_replace %s %.2f → %.2f in %.1f ms", side, state.price, target_price, elapsed_ms)
                new_order = resp.get("newOrderResponse") or {}
                state.order_id = new_order.get("orderId")
                state.price = target_price
                state.quantity = qty
                state.placed_at = time.monotonic()
            except Exception as exc:
                log.error("cancel_replace %s failed: %s", side, exc)
                state.order_id = None  # assume unknown state; will re-place next tick
        else:
            # No existing order – place fresh
            cid = self._new_client_id(side)
            try:
                resp = await self.client.place_limit_order(side, target_price, qty, cid)
                state.order_id = resp.get("orderId")
                state.price = target_price
                state.quantity = qty
                state.placed_at = time.monotonic()
                log.info("Placed %s %s @ %.2f qty=%.6f id=%s", side, Config.SYMBOL, target_price, qty, state.order_id)
            except Exception as exc:
                log.error("place_limit_order %s failed: %s", side, exc)

    # ------------------------------------------------------------------
    # Pricing helpers
    # ------------------------------------------------------------------

    def _fair_mid(self, book: OrderBook) -> float:
        """
        Inventory-adjusted mid price.
        If long → skew quotes down (lower bid & ask) to encourage selling.
        If short → skew quotes up to encourage buying.
        """
        raw_mid = book.mid_price
        if raw_mid is None:
            raise ValueError("book has no mid price")

        btc_value_usdt = self._inventory_btc * raw_mid
        inventory_ratio = btc_value_usdt / max(Config.MAX_POSITION_USDT, 1)
        skew_bps = inventory_ratio * INVENTORY_SKEW_FACTOR
        return raw_mid * (1 - skew_bps / 10_000)

    def _half_spread_bps(self) -> float:
        """
        Half-spread = max(fee-adjusted floor, volatility-scaled spread) / 2.

        The fee floor ensures we never quote tighter than our cost:
          floor = FEE_RATE_BPS (maker fee on each side) + SPREAD_BPS (profit margin)
        """
        volatility = self.ob_ws.candle.volatility_bps
        vol_spread = volatility * 0.1  # use 10% of 5m range as spread

        fee_floor = Config.min_spread_bps()
        full_spread_bps = max(fee_floor, vol_spread)
        return full_spread_bps / 2

    def _round_price(self, price: float) -> float:
        tick = self._tick_size
        return round(round(price / tick) * tick, 2)

    def _round_qty(self, qty: float) -> float:
        step = self._lot_size
        floored = (qty // step) * step
        return round(floored, 6)

    @staticmethod
    def _new_client_id(side: str) -> str:
        return f"mm_{side[:1].lower()}_{uuid.uuid4().hex[:12]}"

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        log.info("Cancelling all open orders on shutdown")
        try:
            await self.client.cancel_all_orders()
        except Exception as exc:
            log.error("Shutdown cancel failed: %s", exc)
        await self.client.close()
