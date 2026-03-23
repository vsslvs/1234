"""
Polymarket CLOB client.

Handles:
- Fetching live fee rates per token (never hard-code)
- EIP-712 order signing with feeRateBps in the payload
- Placing, cancelling, and cancel-replacing maker orders
- One persistent aiohttp session for all REST calls

Order signing spec (Polymarket CLOB v2):
    The signed struct includes feeRateBps. If the value doesn't match
    what the CLOB currently expects, the order is rejected with 400.
    So we always GET /fee-rate?tokenID=... immediately before signing.

Cancel/replace is implemented as two concurrent requests
(cancel + new place) fired in a single asyncio.gather call.
Polymarket's CLOB does not expose a single atomic cancelReplace
endpoint, so we parallelize to minimize wall-clock time.
Target: < 100 ms total for the gather.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp
from eth_account import Account
from eth_account.messages import encode_typed_data

from config import Config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EIP-712 type definitions for Polymarket CLOB order
# ---------------------------------------------------------------------------

_EIP712_DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": Config.CHAIN_ID,
    "verifyingContract": Config.EXCHANGE_ADDRESS,
}

_ORDER_TYPE = [
    {"name": "salt",        "type": "uint256"},
    {"name": "maker",       "type": "address"},
    {"name": "signer",      "type": "address"},
    {"name": "taker",       "type": "address"},
    {"name": "tokenId",     "type": "uint256"},
    {"name": "makerAmount", "type": "uint256"},
    {"name": "takerAmount", "type": "uint256"},
    {"name": "expiration",  "type": "uint256"},
    {"name": "nonce",       "type": "uint256"},
    {"name": "feeRateBps",  "type": "uint256"},
    {"name": "side",        "type": "uint8"},
    {"name": "signatureType", "type": "uint8"},
]

_TAKER_ADDRESS = "0x0000000000000000000000000000000000000000"  # open order

SIDE_BUY  = 0
SIDE_SELL = 1
SIG_TYPE_EOA = 0


@dataclass
class MakerOrder:
    """An open maker order tracked by the bot."""
    order_id: str
    token_id: str
    side: int           # SIDE_BUY or SIDE_SELL
    price: float        # e.g. 0.92
    size_usdc: float
    fee_rate_bps: int
    placed_at: float    # monotonic time


@dataclass
class FeeRate:
    token_id: str
    fee_rate_bps: int
    fetched_at: float   # monotonic — used to decide if cache is stale

    def is_fresh(self, max_age_sec: float = 5.0) -> bool:
        return (time.monotonic() - self.fetched_at) < max_age_sec


class PolymarketClient:
    """
    Async CLOB REST client.

    Usage:
        client = PolymarketClient()
        async with client:
            rate = await client.get_fee_rate(token_id)
            order = await client.place_maker_order(...)
    """

    def __init__(self):
        self._account = Account.from_key(Config.PRIVATE_KEY)
        self._maker_address = self._account.address
        self._session: Optional[aiohttp.ClientSession] = None
        self._fee_cache: Dict[str, FeeRate] = {}

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=5.0)
        connector = aiohttp.TCPConnector(limit=20, keepalive_timeout=30)
        self._session = aiohttp.ClientSession(
            base_url=Config.CLOB_API_URL,
            timeout=timeout,
            connector=connector,
            headers={"Content-Type": "application/json"},
        )
        return self

    async def __aexit__(self, *_):
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Orderbook — best bid/ask from CLOB
    # ------------------------------------------------------------------

    async def get_best_prices(self, token_id: str) -> dict:
        """
        GET /book?token_id={token_id}
        Returns {"best_bid": float|None, "best_ask": float|None}.
        """
        try:
            async with self._session.get("/book", params={"token_id": token_id}) as r:
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
                "get_best_prices failed for %s: [%s] %s",
                token_id[:8], type(exc).__name__, exc,
            )
            return {"best_bid": None, "best_ask": None}

    # ------------------------------------------------------------------
    # Fee rates — always fetched fresh before signing
    # ------------------------------------------------------------------

    async def get_fee_rate(self, token_id: str) -> int:
        """
        GET /fee-rate?tokenID={token_id}
        Returns fee_rate_bps as an int.
        Caches the result for 5 seconds to avoid hammering the endpoint
        while still picking up changes quickly.
        """
        cached = self._fee_cache.get(token_id)
        if cached and cached.is_fresh():
            return cached.fee_rate_bps

        async with self._session.get("/fee-rate", params={"tokenID": token_id}) as r:
            r.raise_for_status()
            data = await r.json()

        bps = int(data.get("feeRateBps", 0))
        self._fee_cache[token_id] = FeeRate(token_id, bps, time.monotonic())
        log.debug("Fee rate for %s: %d bps", token_id, bps)
        return bps

    # ------------------------------------------------------------------
    # Order signing (EIP-712)
    # ------------------------------------------------------------------

    def _sign_order(
        self,
        token_id: str,
        side: int,
        maker_amount: int,
        taker_amount: int,
        fee_rate_bps: int,
        expiration: int = 0,
        nonce: int = 0,
    ) -> Dict[str, Any]:
        """
        Build and sign an EIP-712 order struct.

        feeRateBps is part of the struct — any mismatch between what is
        signed here and what the CLOB currently expects causes rejection.
        We always pass the freshly fetched fee_rate_bps to this method.
        """
        salt = int(time.time() * 1000) % (2**256)

        order = {
            "salt":          salt,
            "maker":         self._maker_address,
            "signer":        self._maker_address,
            "taker":         _TAKER_ADDRESS,
            "tokenId":       int(token_id),
            "makerAmount":   maker_amount,
            "takerAmount":   taker_amount,
            "expiration":    expiration,
            "nonce":         nonce,
            "feeRateBps":    fee_rate_bps,  # <-- signed field
            "side":          side,
            "signatureType": SIG_TYPE_EOA,
        }

        structured_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name",              "type": "string"},
                    {"name": "version",           "type": "string"},
                    {"name": "chainId",           "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": _ORDER_TYPE,
            },
            "domain":      _EIP712_DOMAIN,
            "primaryType": "Order",
            "message":     order,
        }

        message = encode_typed_data(full_message=structured_data)
        sig = self._account.sign_message(message)

        return {
            **{k: str(v) for k, v in order.items()},
            "signature": sig.signature.hex(),
        }

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_maker_order(
        self,
        token_id: str,
        side: int,
        price: float,
        size_usdc: float,
    ) -> MakerOrder:
        """
        Place a maker (limit) order.

        price  : probability price, e.g. 0.92 (= 92 cents per share)
        side   : SIDE_BUY or SIDE_SELL
        size_usdc: USDC amount to spend/receive

        makerAmount = what we are offering (USDC if buying, shares if selling)
        takerAmount = what we want in return

        For BUY:  makerAmount=USDC, takerAmount=shares
                  shares = size_usdc / price
        For SELL: makerAmount=shares, takerAmount=USDC
                  (mirror of buy)
        """
        fee_rate_bps = await self.get_fee_rate(token_id)

        usdc_raw   = int(size_usdc * 1_000_000)
        shares_raw = int(size_usdc / price * 1_000_000)

        if side == SIDE_BUY:
            maker_amount = usdc_raw
            taker_amount = shares_raw
        else:
            maker_amount = shares_raw
            taker_amount = usdc_raw

        signed = self._sign_order(
            token_id=token_id,
            side=side,
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            fee_rate_bps=fee_rate_bps,
        )

        payload = {
            "order":     signed,
            "owner":     self._maker_address,
            "orderType": "GTC",   # Good-Till-Cancelled maker order
        }

        async with self._session.post("/order", json=payload) as r:
            body = await r.json()
            if r.status != 200:
                log.error("place_maker_order failed %s: %s", r.status, body)
                r.raise_for_status()

        order_id = body.get("orderID", body.get("id", ""))
        log.info(
            "Placed %s order id=%s token=%s price=%.4f size=%.2f fee=%dbps",
            "BUY" if side == SIDE_BUY else "SELL",
            order_id, token_id, price, size_usdc, fee_rate_bps,
        )
        return MakerOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=size_usdc,
            fee_rate_bps=fee_rate_bps,
            placed_at=time.monotonic(),
        )

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> bool:
        """DELETE /order/{order_id}. Returns True on success."""
        async with self._session.delete(f"/order/{order_id}") as r:
            if r.status == 200:
                log.debug("Cancelled order %s", order_id)
                return True
            body = await r.json()
            log.warning("cancel_order %s → %s %s", order_id, r.status, body)
            return False

    async def cancel_replace(
        self,
        old_order: MakerOrder,
        new_price: float,
        new_size_usdc: Optional[float] = None,
    ) -> Optional[MakerOrder]:
        """
        Atomic cancel + replace implemented as concurrent REST calls.

        Both requests are fired simultaneously via asyncio.gather.
        Total wall-clock time ≈ max(cancel_rtt, place_rtt) rather than sum.
        Target: < 100 ms on a low-latency VPS.
        """
        t0 = time.monotonic()

        size = new_size_usdc if new_size_usdc is not None else old_order.size_usdc

        cancel_coro = self.cancel_order(old_order.order_id)
        place_coro  = self.place_maker_order(
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
            log.warning("cancel_replace took %.1f ms (budget=%d ms)", elapsed_ms, Config.CANCEL_REPLACE_TIMEOUT_MS)

        if isinstance(new_order_or_exc, Exception):
            log.error("cancel_replace place failed: %s", new_order_or_exc)
            return None

        new_order: MakerOrder = new_order_or_exc

        # cancel_ok is bool (from cancel_order) or an Exception.
        # If the cancel did not succeed, the old order may still be open alongside
        # the newly placed one. Abort by cancelling the new order immediately to
        # prevent double exposure. The caller keeps old_order as the active order
        # and will retry on the next tick.
        cancel_succeeded = isinstance(cancel_ok, bool) and cancel_ok
        if not cancel_succeeded:
            log.warning(
                "cancel_replace: cancel of %s failed (%s) — aborting new order %s "
                "to prevent double exposure",
                old_order.order_id,
                cancel_ok,
                new_order.order_id,
            )
            await self.cancel_order(new_order.order_id)
            return None

        log.debug("cancel_replace completed in %.1f ms", elapsed_ms)
        return new_order

    # ------------------------------------------------------------------
    # Query open orders
    # ------------------------------------------------------------------

    async def get_open_orders(self, token_id: Optional[str] = None) -> List[dict]:
        """GET /orders?maker={address}&tokenID={token_id}"""
        params: Dict[str, str] = {"maker": self._maker_address}
        if token_id:
            params["tokenID"] = token_id
        async with self._session.get("/orders", params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def cancel_all_orders(self) -> None:
        """Cancel every open order for this account (called on shutdown)."""
        try:
            orders = await self.get_open_orders()
        except Exception as exc:
            log.warning("Could not fetch open orders on shutdown: %s", exc)
            return
        if not orders:
            return
        tasks = [
            self.cancel_order(o.get("id") or o.get("orderID", ""))
            for o in orders
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        cancelled = sum(1 for r in results if r is True)
        log.info("Cancelled %d / %d open orders on shutdown", cancelled, len(orders))

    # ------------------------------------------------------------------
    # Approval check (one-time setup)
    # ------------------------------------------------------------------

    async def check_approvals(self) -> None:
        """
        Verify that the exchange contract is approved for USDC and
        conditional tokens. Log a warning if not — the user must run the
        one-time approval transaction before the bot can trade.
        """
        async with self._session.get(
            "/auth/approvals",
            params={"address": self._maker_address},
        ) as r:
            if r.status != 200:
                log.warning("Could not check approvals (status %s)", r.status)
                return
            data = await r.json()

        if not data.get("usdcApproved"):
            log.error(
                "USDC not approved for exchange contract %s. "
                "Run the one-time approval before trading.",
                Config.EXCHANGE_ADDRESS,
            )
        if not data.get("conditionalTokenApproved"):
            log.error(
                "Conditional tokens not approved. "
                "Run the one-time approval before trading.",
            )
