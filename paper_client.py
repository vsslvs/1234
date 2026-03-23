"""
Paper trading client — drop-in replacement for PolymarketClient.

Simulates order placement, cancellation, and resolution without
sending any real transactions.  Tracks virtual balance and P&L.

Fill simulation
---------------
Orders are NOT filled instantly.  They are placed as "pending" resting
limit orders.  The MarketMaker's CLOB polling loop calls back to check
if a pending order would have filled based on real Polymarket ask prices.
Only filled orders participate in resolution.

Balance accounting:
  - place: balance -= size  (collateral locked)
  - cancel: balance += size  (collateral released)
  - resolve(win):  balance += (payout - size)  i.e. the profit delta
  - resolve(loss): balance += (-size)           i.e. the loss delta
"""
import logging
import time
import uuid
from collections import deque
from typing import Deque, Dict, List, Optional

import aiohttp

from config import Config
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

    async def __aenter__(self):
        self._http = aiohttp.ClientSession(
            base_url=Config.CLOB_API_URL,
            timeout=aiohttp.ClientTimeout(total=5.0),
        )
        log.info(
            "PAPER TRADING mode | virtual balance: %.2f USDC",
            self.balance,
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
        """No approvals needed in paper mode."""
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
        Fills are determined by MarketMaker._check_paper_fills().
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

    def resolve_trade(self, won: bool, size_usdc: float, entry_price: float) -> float:
        """
        Resolve a paper trade outcome and update balance.

        The stake was already deducted on place and refunded on cancel.
        This method applies only the P&L delta:
          win:  balance += (payout - size)  where payout = size/price
          loss: balance += (-size)
        """
        if won:
            shares = size_usdc / entry_price
            payout = shares * 1.0  # each winning share pays $1
            pnl = payout - size_usdc
        else:
            pnl = -size_usdc

        self.balance += pnl
        self._total_pnl += pnl
        self._trade_count += 1
        return pnl
