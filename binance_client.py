"""
Async Binance REST client.

All signed endpoints include:
  - timestamp
  - recvWindow
  - feeRateBps   (passed as a query param so it is part of the HMAC signature)

The cancel-replace cycle uses POST /api/v3/order/cancelReplace which is a
single round-trip, keeping the total latency well under 100 ms on a co-located
or low-latency connection.
"""
import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import aiohttp

from config import Config

log = logging.getLogger(__name__)

_RECV_WINDOW = 5000  # ms – tight window to reject stale requests


def _sign(params: Dict[str, Any]) -> str:
    """Return HMAC-SHA256 hex signature over URL-encoded params."""
    query = urlencode(params)
    return hmac.new(
        Config.API_SECRET.encode(),
        query.encode(),
        hashlib.sha256,
    ).hexdigest()


def _build_signed_params(extra: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge base signed fields with caller-supplied params and append signature.

    feeRateBps is always included so it becomes part of the canonical
    signed string – any tampering of the fee tier would invalidate the sig.
    """
    params: Dict[str, Any] = {
        "timestamp": int(time.time() * 1000),
        "recvWindow": _RECV_WINDOW,
        "feeRateBps": Config.FEE_RATE_BPS,
        **extra,
    }
    params["signature"] = _sign(params)
    return params


class BinanceClient:
    """
    Thin async wrapper around Binance Spot REST API.

    Lifetime: one session per bot instance.  Call `await client.close()`
    on shutdown to release the underlying TCP connections.
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=20,
                enable_cleanup_closed=True,
                keepalive_timeout=30,
            )
            timeout = aiohttp.ClientTimeout(
                total=Config.CANCEL_REPLACE_TIMEOUT_MS / 1000
            )
            self._session = aiohttp.ClientSession(
                base_url=Config.REST_BASE,
                connector=connector,
                timeout=timeout,
                headers={
                    "X-MBX-APIKEY": Config.API_KEY,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Public (unsigned) endpoints
    # ------------------------------------------------------------------

    async def get_exchange_info(self) -> Dict:
        session = await self._get_session()
        async with session.get("/api/v3/exchangeInfo", params={"symbol": Config.SYMBOL}) as r:
            r.raise_for_status()
            return await r.json()

    async def get_server_time(self) -> int:
        session = await self._get_session()
        async with session.get("/api/v3/time") as r:
            r.raise_for_status()
            data = await r.json()
            return data["serverTime"]

    # ------------------------------------------------------------------
    # Signed endpoints
    # ------------------------------------------------------------------

    async def get_account(self) -> Dict:
        params = _build_signed_params({})
        session = await self._get_session()
        async with session.get("/api/v3/account", params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def get_open_orders(self) -> list:
        params = _build_signed_params({"symbol": Config.SYMBOL})
        session = await self._get_session()
        async with session.get("/api/v3/openOrders", params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def place_limit_order(
        self,
        side: str,        # "BUY" or "SELL"
        price: float,
        quantity: float,
        client_order_id: Optional[str] = None,
    ) -> Dict:
        """
        Place a maker-only LIMIT order (timeInForce=GTX = Post-Only).
        GTX rejects immediately if the order would match (i.e. ensures maker).
        """
        extra: Dict[str, Any] = {
            "symbol": Config.SYMBOL,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTX",   # Post-Only / Good Till Crossing
            "price": f"{price:.2f}",
            "quantity": f"{quantity:.6f}",
        }
        if client_order_id:
            extra["newClientOrderId"] = client_order_id

        params = _build_signed_params(extra)
        session = await self._get_session()
        async with session.post("/api/v3/order", data=params) as r:
            body = await r.json()
            if r.status != 200:
                log.error("place_limit_order error %s: %s", r.status, body)
            r.raise_for_status()
            return body

    async def cancel_order(self, order_id: int) -> Dict:
        params = _build_signed_params({"symbol": Config.SYMBOL, "orderId": order_id})
        session = await self._get_session()
        async with session.delete("/api/v3/order", params=params) as r:
            body = await r.json()
            if r.status != 200:
                log.warning("cancel_order %s error %s: %s", order_id, r.status, body)
            return body

    async def cancel_replace_order(
        self,
        cancel_order_id: int,
        side: str,
        price: float,
        quantity: float,
        client_order_id: Optional[str] = None,
    ) -> Dict:
        """
        Single-round-trip cancel + replace via POST /api/v3/order/cancelReplace.

        cancelReplaceMode=STOP_ON_FAILURE – if cancel fails, new order is NOT sent.
        This keeps book state consistent and is the fastest path (<100 ms).
        """
        extra: Dict[str, Any] = {
            "symbol": Config.SYMBOL,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTX",
            "cancelReplaceMode": "STOP_ON_FAILURE",
            "cancelOrderId": cancel_order_id,
            "price": f"{price:.2f}",
            "quantity": f"{quantity:.6f}",
        }
        if client_order_id:
            extra["newClientOrderId"] = client_order_id

        params = _build_signed_params(extra)
        session = await self._get_session()
        t0 = time.monotonic()
        async with session.post("/api/v3/order/cancelReplace", data=params) as r:
            elapsed_ms = (time.monotonic() - t0) * 1000
            body = await r.json()
            if elapsed_ms > Config.CANCEL_REPLACE_TIMEOUT_MS:
                log.warning("cancel_replace took %.1f ms (> %d ms budget)", elapsed_ms, Config.CANCEL_REPLACE_TIMEOUT_MS)
            if r.status not in (200, 400):
                log.error("cancel_replace error %s: %s", r.status, body)
                r.raise_for_status()
            return body

    async def cancel_all_orders(self) -> list:
        """Cancel all open orders for the symbol (used on shutdown)."""
        params = _build_signed_params({"symbol": Config.SYMBOL})
        session = await self._get_session()
        async with session.delete("/api/v3/openOrders", params=params) as r:
            body = await r.json()
            log.info("Cancelled all orders: %s", body)
            return body if isinstance(body, list) else []
