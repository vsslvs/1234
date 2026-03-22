"""
Paper trading client — drop-in replacement for PolymarketClient.

Simulates order placement, cancellation, and resolution without
sending any real transactions. Tracks virtual balance and P&L.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import Config
from polymarket_client import MakerOrder, SIDE_BUY

log = logging.getLogger(__name__)


class PaperClient:
    """
    Mimics the PolymarketClient interface but operates entirely in memory.
    Orders are "filled" immediately at the requested price (optimistic fill).
    """

    def __init__(self, initial_balance: float = Config.PAPER_BALANCE_USDC):
        self.balance: float = initial_balance
        self.initial_balance: float = initial_balance
        self._open_orders: Dict[str, MakerOrder] = {}
        self._filled_orders: List[MakerOrder] = []
        self._total_pnl: float = 0.0

    async def __aenter__(self):
        log.info(
            "PAPER TRADING mode | virtual balance: %.2f USDC",
            self.balance,
        )
        return self

    async def __aexit__(self, *_):
        log.info(
            "Paper session ended | balance: %.2f USDC | P&L: %+.2f USDC | trades: %d",
            self.balance,
            self.balance - self.initial_balance,
            len(self._filled_orders),
        )

    async def check_approvals(self) -> None:
        """No approvals needed in paper mode."""
        log.info("Paper mode — skipping approval check")

    async def place_maker_order(
        self,
        token_id: str,
        side: int,
        price: float,
        size_usdc: float,
    ) -> Optional[MakerOrder]:
        """Simulate placing a maker order."""
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
        self.balance -= size_usdc
        self._filled_orders.append(order)

        log.info(
            "Paper ORDER | %s %s @ %.4f | size=%.2f USDC | balance=%.2f",
            "BUY" if side == SIDE_BUY else "SELL",
            token_id[:8] + "...",
            price,
            size_usdc,
            self.balance,
        )
        return order

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a paper order and refund balance."""
        order = self._open_orders.pop(order_id, None)
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
        """Cancel all open paper orders."""
        order_ids = list(self._open_orders.keys())
        for oid in order_ids:
            await self.cancel_order(oid)
        if order_ids:
            log.info("Paper: cancelled %d open orders", len(order_ids))

    async def get_open_orders(self, token_id: Optional[str] = None) -> List[dict]:
        """Return open paper orders."""
        orders = list(self._open_orders.values())
        if token_id:
            orders = [o for o in orders if o.token_id == token_id]
        return [{"id": o.order_id, "tokenID": o.token_id} for o in orders]

    def resolve_trade(self, won: bool, size_usdc: float, entry_price: float) -> float:
        """
        Resolve a paper trade outcome and update balance.
        Returns the P&L amount.
        """
        if won:
            shares = size_usdc / entry_price
            payout = shares * 1.0  # each winning share pays $1
            pnl = payout - size_usdc
        else:
            pnl = -size_usdc

        # Only apply the P&L delta here. The stake itself is refunded
        # separately when cancel_all_orders runs during window rollover.
        self.balance += pnl
        self._total_pnl += pnl
        return pnl
