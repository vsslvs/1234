"""
Paper trading client for the Polymarket BTC market maker.

Implements the same interface as PolymarketClient but simulates
all order operations locally without touching Polymarket's CLOB.
Uses live Binance data for realistic price movements.

Usage:
    client = PaperClient()
    async with client:
        order = await client.place_maker_order(...)
"""
import asyncio
import logging
import random
import time
import uuid
from typing import Dict, List, Optional

from polymarket_client import MakerOrder

log = logging.getLogger(__name__)


class PaperClient:
    """
    Mock order executor that simulates fills locally.

    All orders are filled immediately at the requested price (optimistic model).
    """

    def __init__(self):
        self._open_orders: Dict[str, MakerOrder] = {}
        self._total_placed: int = 0
        self._total_cancelled: int = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    # ------------------------------------------------------------------
    # Order placement (simulated)
    # ------------------------------------------------------------------

    async def place_maker_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usdc: float,
    ) -> MakerOrder:
        """Simulate placing a maker order — fills immediately."""
        await asyncio.sleep(random.uniform(0.005, 0.025))

        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        size_shares = size_usdc / price if price > 0 else 0

        order = MakerOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size_shares,
            size_usdc=size_usdc,
            placed_at=time.monotonic(),
        )

        self._open_orders[order_id] = order
        self._total_placed += 1

        log.info(
            "[PAPER] Placed %s order id=%s price=%.4f size=%.2f USDC",
            side, order_id, price, size_usdc,
        )
        return order

    # ------------------------------------------------------------------
    # Cancel (simulated)
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> bool:
        """Simulate cancelling an order — always succeeds."""
        await asyncio.sleep(random.uniform(0.002, 0.010))

        if order_id in self._open_orders:
            del self._open_orders[order_id]
            self._total_cancelled += 1
            log.debug("[PAPER] Cancelled order %s", order_id)
            return True

        log.debug("[PAPER] Cancel order %s — not found (already resolved)", order_id)
        return True

    async def cancel_replace(
        self,
        old_order: MakerOrder,
        new_price: float,
        new_size_usdc: Optional[float] = None,
    ) -> Optional[MakerOrder]:
        """Simulate cancel + replace."""
        size = new_size_usdc if new_size_usdc is not None else old_order.size_usdc

        cancel_coro = self.cancel_order(old_order.order_id)
        place_coro = self.place_maker_order(
            token_id=old_order.token_id,
            side=old_order.side,
            price=new_price,
            size_usdc=size,
        )

        cancel_ok, new_order_or_exc = await asyncio.gather(
            cancel_coro, place_coro, return_exceptions=True,
        )

        if isinstance(new_order_or_exc, Exception):
            log.error("[PAPER] cancel_replace place failed: %s", new_order_or_exc)
            return None

        return new_order_or_exc

    # ------------------------------------------------------------------
    # Query & cleanup
    # ------------------------------------------------------------------

    async def get_open_orders(self, token_id: Optional[str] = None) -> List[dict]:
        """Return simulated open orders."""
        orders = list(self._open_orders.values())
        if token_id:
            orders = [o for o in orders if o.token_id == token_id]
        return [
            {"id": o.order_id, "tokenID": o.token_id, "price": o.price}
            for o in orders
        ]

    async def cancel_all_orders(self) -> None:
        """Cancel all simulated open orders."""
        count = len(self._open_orders)
        self._open_orders.clear()
        if count:
            log.info("[PAPER] Cancelled %d open orders on shutdown", count)

    async def check_approvals(self) -> None:
        """No-op for paper trading."""
        log.info("[PAPER] Approval check skipped (paper mode)")

    def summary(self) -> dict:
        """Return paper trading session summary."""
        return {
            "total_placed": self._total_placed,
            "total_cancelled": self._total_cancelled,
            "open_orders": len(self._open_orders),
        }
