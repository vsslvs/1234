"""
Polymarket CLOB client — wraps the official py-clob-client library.

The official library handles:
- L2 HMAC authentication (API key/secret/passphrase)
- EIP-712 order signing via poly_eip712_structs
- neg_risk exchange address resolution
- Tick-size validation and amount rounding

This module wraps the synchronous library in asyncio.to_thread() calls
and exposes the same interface expected by MarketMaker.

Cancel/replace is implemented as two concurrent requests
(cancel + new place) fired in a single asyncio.gather call.
Target: < 100 ms total for the gather.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

from config import Config

log = logging.getLogger(__name__)

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

# Integer constants kept for backward compat with MarketMaker imports
_SIDE_INT_BUY = 0
_SIDE_INT_SELL = 1


@dataclass
class MakerOrder:
    """An open maker order tracked by the bot."""
    order_id: str
    token_id: str
    side: str           # "BUY" or "SELL"
    price: float        # e.g. 0.92
    size: float         # size in shares
    size_usdc: float    # approximate USDC value
    placed_at: float    # monotonic time


class PolymarketClient:
    """
    Async wrapper around the official py-clob-client.

    Derives API credentials automatically on first use.

    Usage:
        client = PolymarketClient()
        async with client:
            order = await client.place_maker_order(...)
    """

    def __init__(self):
        self._clob: Optional[ClobClient] = None

    async def __aenter__(self):
        # Initialize the synchronous CLOB client
        self._clob = ClobClient(
            host=Config.CLOB_API_URL,
            chain_id=Config.CHAIN_ID,
            key=Config.PRIVATE_KEY,
        )

        # Check if API creds are provided in config
        if Config.CLOB_API_KEY and Config.CLOB_API_SECRET and Config.CLOB_API_PASSPHRASE:
            creds = ApiCreds(
                api_key=Config.CLOB_API_KEY,
                api_secret=Config.CLOB_API_SECRET,
                api_passphrase=Config.CLOB_API_PASSPHRASE,
            )
            self._clob.set_api_creds(creds)
            log.info("Using provided CLOB API credentials")
        else:
            # Derive credentials from wallet (creates on first call)
            log.info("Deriving CLOB API credentials from wallet...")
            creds = await asyncio.to_thread(
                self._clob.create_or_derive_api_creds
            )
            self._clob.set_api_creds(creds)
            log.info(
                "CLOB API credentials derived. To skip this step, add to .env:\n"
                "  CLOB_API_KEY=%s\n"
                "  CLOB_API_SECRET=%s\n"
                "  CLOB_API_PASSPHRASE=%s",
                creds.api_key, creds.api_secret, creds.api_passphrase,
            )

        return self

    async def __aexit__(self, *_):
        self._clob = None

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_maker_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usdc: float,
    ) -> MakerOrder:
        """
        Place a maker (limit) order.

        price     : probability price, e.g. 0.92
        side      : "BUY" or "SELL"
        size_usdc : USDC amount to spend

        For BUY: size in shares = size_usdc / price
        """
        # Convert USDC to shares for OrderArgs
        if price <= 0 or price >= 1:
            raise ValueError(f"Price must be in (0, 1), got {price}")

        size_shares = size_usdc / price

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size_shares,
            side=side,
        )

        # create_order signs locally, post_order sends to CLOB
        # Both are sync — run in thread pool
        def _create_and_post():
            signed = self._clob.create_order(order_args)
            resp = self._clob.post_order(
                signed,
                orderType=OrderType.GTC,
            )
            return resp

        t0 = time.monotonic()
        resp = await asyncio.to_thread(_create_and_post)
        elapsed_ms = (time.monotonic() - t0) * 1000

        order_id = resp.get("orderID", resp.get("id", ""))

        log.info(
            "Placed %s order id=%s price=%.4f size=%.2f shares (%.2f USDC) in %.0fms",
            side, order_id, price, size_shares, size_usdc, elapsed_ms,
        )

        return MakerOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size_shares,
            size_usdc=size_usdc,
            placed_at=time.monotonic(),
        )

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID. Returns True on success."""
        try:
            await asyncio.to_thread(self._clob.cancel, order_id)
            log.debug("Cancelled order %s", order_id)
            return True
        except Exception as exc:
            log.warning("cancel_order %s failed: %s", order_id, exc)
            return False

    async def cancel_replace(
        self,
        old_order: MakerOrder,
        new_price: float,
        new_size_usdc: Optional[float] = None,
    ) -> Optional[MakerOrder]:
        """
        Cancel + replace implemented as concurrent REST calls.

        Both requests are fired simultaneously via asyncio.gather.
        Total wall-clock time ≈ max(cancel_rtt, place_rtt).
        """
        t0 = time.monotonic()

        size = new_size_usdc if new_size_usdc is not None else old_order.size_usdc

        cancel_coro = self.cancel_order(old_order.order_id)
        place_coro = self.place_maker_order(
            token_id=old_order.token_id,
            side=old_order.side,
            price=new_price,
            size_usdc=size,
        )

        cancel_ok, new_order_or_exc = await asyncio.gather(
            cancel_coro, place_coro, return_exceptions=True
        )

        elapsed_ms = (time.monotonic() - t0) * 1000
        if elapsed_ms > Config.CANCEL_REPLACE_TIMEOUT_MS:
            log.warning(
                "cancel_replace took %.1f ms (budget=%d ms)",
                elapsed_ms, Config.CANCEL_REPLACE_TIMEOUT_MS,
            )

        if isinstance(new_order_or_exc, Exception):
            log.error("cancel_replace place failed: %s", new_order_or_exc)
            return None

        new_order: MakerOrder = new_order_or_exc

        cancel_succeeded = isinstance(cancel_ok, bool) and cancel_ok
        if not cancel_succeeded:
            log.warning(
                "cancel_replace: cancel of %s failed (%s) — aborting new order %s",
                old_order.order_id, cancel_ok, new_order.order_id,
            )
            await self.cancel_order(new_order.order_id)
            return None

        log.debug("cancel_replace completed in %.1f ms", elapsed_ms)
        return new_order

    # ------------------------------------------------------------------
    # Query / bulk cancel
    # ------------------------------------------------------------------

    async def get_open_orders(self, token_id: Optional[str] = None) -> List[dict]:
        """Fetch open orders from CLOB."""
        try:
            orders = await asyncio.to_thread(self._clob.get_orders)
            if token_id:
                orders = [
                    o for o in orders
                    if o.get("asset_id") == token_id
                ]
            return orders
        except Exception as exc:
            log.warning("get_open_orders failed: %s", exc)
            return []

    async def cancel_all_orders(self) -> None:
        """Cancel all open orders."""
        try:
            await asyncio.to_thread(self._clob.cancel_all)
            log.info("Cancelled all open orders")
        except Exception as exc:
            log.warning("cancel_all failed: %s", exc)

    # ------------------------------------------------------------------
    # Approval check
    # ------------------------------------------------------------------

    async def check_approvals(self) -> None:
        """Log a reminder about token approvals."""
        log.info(
            "Ensure USDC and conditional tokens are approved for trading. "
            "See Polymarket docs for one-time approval setup.",
        )
