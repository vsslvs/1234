"""
Paper trading client — drop-in replacement for PolymarketClient.

Simulates order placement, cancellation, and resolution without
sending any real transactions.  Tracks virtual balance and P&L.

Fill simulation (realistic)
---------------------------
Orders are NOT filled instantly.  They are placed as "pending" resting
limit orders.  The MarketMaker's CLOB polling loop calls back to check
if a pending order would have filled based on real Polymarket ask prices.

Realism features:
  - Slippage: random adverse slippage added to fill price (configurable BPS)
  - Latency: simulated order placement delay prevents instant reaction
  - Partial fills: large orders may only partially fill based on market depth
  - Price improvement: fill at min(our_bid, market_ask) like real limit orders
  - Fee simulation: exact Polymarket fee formula applied to every trade

Balance accounting:
  - place: balance -= size  (collateral locked)
  - cancel: balance += size  (collateral released)
  - resolve(win):  balance += (payout - size - fee)
  - resolve(loss): balance += (-size)
"""
import logging
import random
import time
import uuid
from collections import deque
from typing import Deque, Dict, List, Optional

import aiohttp

from config import Config
from market_calculator import compute_fee
from polymarket_client import MakerOrder, SIDE_BUY

log = logging.getLogger(__name__)


class PaperClient:
    """
    Mimics the PolymarketClient interface but operates entirely in memory.
    Orders start as pending and are filled only when market conditions confirm.
    """

    def __init__(self, initial_balance: float = Config.PAPER_BALANCE_USDC):
        self.balance: float = initial_balance
        self.initial_balance: float = initial_balance
        self._open_orders: Dict[str, MakerOrder] = {}
        self._trade_count: int = 0
        self._total_pnl: float = 0.0
        self._http: Optional[aiohttp.ClientSession] = None

        # Latency simulation: orders placed before this time are "in flight"
        self._order_placed_at: Dict[str, float] = {}  # order_id → monotonic time

        # Fill history for analysis
        self._fill_history: Deque[dict] = deque(maxlen=200)

    async def __aenter__(self):
        self._http = aiohttp.ClientSession(
            base_url=Config.CLOB_API_URL,
            timeout=aiohttp.ClientTimeout(total=5.0),
        )
        log.info(
            "PAPER TRADING mode | virtual balance: %.2f USDC | "
            "slippage: %.1f bps | latency: %.0f ms | partial fills: %s",
            self.balance,
            Config.PAPER_SLIPPAGE_BPS,
            Config.PAPER_LATENCY_MS,
            "ON" if Config.PAPER_PARTIAL_FILL_ENABLED else "OFF",
        )
        return self

    async def __aexit__(self, *_):
        if self._http and not self._http.closed:
            await self._http.close()
        log.info(
            "Paper session ended | balance: %.2f USDC | P&L: %+.2f USDC | trades: %d",
            self.balance,
            self.balance - self.initial_balance,
            self._trade_count,
        )

    async def check_approvals(self) -> None:
        log.info("Paper mode — skipping approval check")

    async def place_maker_order(
        self,
        token_id: str,
        side: int,
        price: float,
        size_usdc: float,
    ) -> Optional[MakerOrder]:
        """
        Simulate placing a maker order.
        Deducts balance as collateral (released on cancel).
        The order is PENDING — it does NOT fill instantly.
        """
        if size_usdc > self.balance:
            log.warning(
                "Paper: insufficient balance (%.2f < %.2f) — order rejected",
                self.balance, size_usdc,
            )
            return None

        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        order = MakerOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=size_usdc,
            fee_rate_bps=0,
            placed_at=time.monotonic(),
        )
        self._open_orders[order_id] = order
        self.balance -= size_usdc  # lock collateral

        # Record placement time for latency simulation
        self._order_placed_at[order_id] = time.monotonic()

        log.info(
            "Paper ORDER (pending) | %s %s @ %.4f | size=%.2f USDC | balance=%.2f",
            "BUY" if side == SIDE_BUY else "SELL",
            token_id[:8] + "...",
            price,
            size_usdc,
            self.balance,
        )
        return order

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a paper order and release collateral."""
        order = self._open_orders.pop(order_id, None)
        self._order_placed_at.pop(order_id, None)
        if order:
            self.balance += order.size_usdc
            log.debug("Paper CANCEL | %s | refunded %.2f", order_id, order.size_usdc)
            return True
        return False

    async def cancel_replace(
        self,
        old_order: MakerOrder,
        new_price: float,
    ) -> Optional[MakerOrder]:
        """Cancel old order and place new one at updated price."""
        await self.cancel_order(old_order.order_id)
        return await self.place_maker_order(
            token_id=old_order.token_id,
            side=old_order.side,
            price=new_price,
            size_usdc=old_order.size_usdc,
        )

    async def cancel_all_orders(self) -> None:
        order_ids = list(self._open_orders.keys())
        for oid in order_ids:
            await self.cancel_order(oid)
        if order_ids:
            log.info("Paper: cancelled %d open orders", len(order_ids))

    async def get_open_orders(self, token_id: Optional[str] = None) -> List[dict]:
        orders = list(self._open_orders.values())
        if token_id:
            orders = [o for o in orders if o.token_id == token_id]
        return [{"id": o.order_id, "tokenID": o.token_id} for o in orders]

    async def get_best_prices(self, token_id: str) -> dict:
        """Fetch real best bid/ask from Polymarket CLOB (needed even in paper mode)."""
        try:
            async with self._http.get("/book", params={"token_id": token_id}) as r:
                r.raise_for_status()
                data = await r.json()

            best_bid = None
            best_ask = None
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids:
                best_bid = max(float(b["price"]) for b in bids)
            if asks:
                best_ask = min(float(a["price"]) for a in asks)

            return {"best_bid": best_bid, "best_ask": best_ask}
        except aiohttp.ClientResponseError as exc:
            log.warning(
                "get_best_prices HTTP %s for %s: %s",
                exc.status, token_id[:8], exc.message,
            )
            return {"best_bid": None, "best_ask": None}
        except Exception as exc:
            log.warning(
                "get_best_prices failed for %s: [%s] %r",
                token_id[:8], type(exc).__name__, exc,
            )
            return {"best_bid": None, "best_ask": None}

    # ------------------------------------------------------------------
    # Fill simulation
    # ------------------------------------------------------------------

    def check_fill(self, order: MakerOrder, market_ask: float) -> Optional[dict]:
        """
        Check if a pending order would have filled given the current market ask.

        Returns fill info dict if filled, None otherwise.

        Fill conditions:
        1. Our bid >= market ask (maker fills when crossed)
        2. Order has been "in flight" for at least PAPER_LATENCY_MS
           (simulates network + exchange matching latency)

        Fill price calculation:
        - Base: min(our_bid, market_ask) — price improvement like real markets
        - Slippage: random adverse adjustment (0 to PAPER_SLIPPAGE_BPS)
        """
        if market_ask is None or market_ask <= 0:
            return None

        # Condition 1: price crossed
        if order.price < market_ask:
            return None

        # Condition 2: latency simulation
        placed_at = self._order_placed_at.get(order.order_id, 0)
        latency_sec = Config.PAPER_LATENCY_MS / 1000.0
        if time.monotonic() - placed_at < latency_sec:
            return None

        # Fill price with price improvement
        base_fill_price = min(order.price, market_ask)

        # Random adverse slippage: 0 to PAPER_SLIPPAGE_BPS
        slippage_bps = random.uniform(0, Config.PAPER_SLIPPAGE_BPS)
        slippage = base_fill_price * slippage_bps / 10_000
        fill_price = base_fill_price + slippage  # adverse = higher price for buyer

        # Cap fill price at our bid (slippage can't make us pay more than bid)
        fill_price = min(fill_price, order.price)
        fill_price = round(fill_price, 4)

        # Partial fill simulation
        fill_fraction = 1.0
        if Config.PAPER_PARTIAL_FILL_ENABLED:
            # Larger orders are less likely to fully fill
            # Base fill rate: 90% for small ($50), decreasing for larger
            fill_fraction = min(1.0, 0.9 + random.uniform(-0.15, 0.1))
            # Very tight fills (bid barely above ask) are less likely to fully fill
            spread_tightness = (order.price - market_ask) / market_ask
            if spread_tightness < 0.005:  # within 0.5%
                fill_fraction *= random.uniform(0.6, 1.0)
            fill_fraction = max(0.5, min(1.0, fill_fraction))

        fill_size_usdc = round(order.size_usdc * fill_fraction, 2)

        fill_info = {
            "fill_price": fill_price,
            "fill_size_usdc": fill_size_usdc,
            "fill_fraction": fill_fraction,
            "slippage_bps": slippage_bps,
            "latency_ms": (time.monotonic() - placed_at) * 1000,
        }

        # Record fill
        self._fill_history.append({
            **fill_info,
            "order_price": order.price,
            "market_ask": market_ask,
            "timestamp": time.time(),
        })

        return fill_info

    def resolve_trade(self, won: bool, size_usdc: float, entry_price: float) -> float:
        """
        Resolve a paper trade outcome and update balance.

        Includes Polymarket fee simulation using the exact formula.
        The stake was already deducted on place and refunded on cancel.
        This method applies only the P&L delta.
        """
        shares = size_usdc / entry_price
        fee = compute_fee(shares, entry_price)

        if won:
            payout = shares * 1.0
            pnl = payout - size_usdc - fee
        else:
            pnl = -size_usdc

        self.balance += pnl
        self._total_pnl += pnl
        self._trade_count += 1
        return pnl

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def fill_stats(self) -> dict:
        """Summary statistics of paper fill quality."""
        if not self._fill_history:
            return {}
        fills = list(self._fill_history)
        avg_slippage = sum(f["slippage_bps"] for f in fills) / len(fills)
        avg_fill_frac = sum(f["fill_fraction"] for f in fills) / len(fills)
        avg_latency = sum(f["latency_ms"] for f in fills) / len(fills)
        return {
            "total_fills": len(fills),
            "avg_slippage_bps": round(avg_slippage, 2),
            "avg_fill_fraction": round(avg_fill_frac, 3),
            "avg_latency_ms": round(avg_latency, 1),
        }
