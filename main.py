"""
Polymarket BTC 5-minute market maker bot — entry point.

Quick start:
    cp .env.example .env
    # fill in POLYMARKET_PRIVATE_KEY
    pip install -r requirements.txt
    python main.py

What this does:
  1. Connects to Binance WebSocket for live BTC/USDT order book
  2. Fetches active 5-minute BTC markets from Polymarket Gamma API
  3. Computes directional signal (P_up) from Binance price vs window open
  4. During the last 10s of each window, places maker orders on the
     high-confidence side at 92-95 cents
  5. Refreshes quotes every 200ms via cancel/replace (<100ms target)
  6. Cancels all orders 2s before close to avoid post-resolution fills
  7. Earns USDC maker rebates funded by taker fees

Key design points:
  - feeRateBps is fetched live from /fee-rate endpoint before EVERY order
    and included in the EIP-712 signed struct — never hard-coded
  - No 500ms taker delay means stale quotes are dangerous:
    cancel/replace fires as parallel requests via asyncio.gather
  - Taker fee at p=50% is ~1.56% — we only quote when P(up) > 80%
    or P(down) > 80% to avoid quoting into adverse selection
"""
import asyncio
import logging
import os
import signal
import sys

from config import Config
from dashboard import DashboardLogHandler, EventBus, start_dashboard
from market_calculator import MarketCalculator
from market_maker import MarketMaker
from polymarket_client import PolymarketClient
from ws_orderbook import OrderBookWS


def _setup_logging() -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s – %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


async def _main() -> None:
    _setup_logging()
    log = logging.getLogger("main")

    # Set up dashboard event bus and logging handler
    event_bus = EventBus()
    dash_handler = DashboardLogHandler(event_bus)
    dash_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(dash_handler)

    # Start dashboard web server
    dash_runner = await start_dashboard(event_bus)

    log.info(
        "BTC 5m market maker starting | wallet=%s...%s",
        Config.PRIVATE_KEY[:6],
        Config.PRIVATE_KEY[-4:],
    )

    # Binance WS for live BTC price feed
    ob_ws = OrderBookWS()
    ws_task = asyncio.create_task(ob_ws.run(), name="binance-ws")

    # Wait briefly for first order book snapshot
    for _ in range(20):
        if ob_ws.book.mid_price is not None:
            break
        await asyncio.sleep(0.1)
    if ob_ws.book.mid_price is None:
        log.warning("Binance WS connected but no price yet — proceeding anyway")

    log.info("BTC mid-price: %.2f", ob_ws.book.mid_price or 0)

    async with PolymarketClient() as client:
        async with MarketCalculator(ob_ws) as calc:
            mm = MarketMaker(client, calc, ob_ws, event_bus=event_bus)

            loop = asyncio.get_running_loop()
            stop_event = asyncio.Event()

            def _handle_signal(sig):
                log.info("Signal %s received — stopping", sig.name)
                stop_event.set()

            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _handle_signal, sig)

            mm_task = asyncio.create_task(mm.run(), name="market-maker")

            await asyncio.wait(
                [mm_task, asyncio.create_task(stop_event.wait())],
                return_when=asyncio.FIRST_COMPLETED,
            )

            log.info("Stopping — cancelling all open orders")
            await mm.stop()
            mm_task.cancel()
            ws_task.cancel()
            try:
                await asyncio.gather(mm_task, ws_task, return_exceptions=True)
            except asyncio.CancelledError:
                pass

    await dash_runner.cleanup()
    log.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
