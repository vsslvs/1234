"""
BTC Market Maker Bot – entry point.

Usage:
    cp .env.example .env          # fill in API keys
    pip install -r requirements.txt
    python main.py

The bot will:
  1. Load config from .env
  2. Connect to Binance order book WebSocket
  3. Place/refresh maker orders on both sides every QUOTE_REFRESH_MS
  4. Cancel/replace stale quotes via a single round-trip (<100 ms)
  5. Cancel all orders cleanly on Ctrl-C / SIGTERM
"""
import asyncio
import logging
import signal
import sys

from config import Config
from market_maker import MarketMaker


def _setup_logging() -> None:
    level = getattr(logging, Config._get("LOG_LEVEL", "INFO"), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s – %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


async def _main() -> None:
    _setup_logging()
    log = logging.getLogger("main")
    log.info(
        "BTC Market Maker starting | symbol=%s spread=%dbps fee=%dbps refresh=%dms",
        Config.SYMBOL,
        Config.SPREAD_BPS,
        Config.FEE_RATE_BPS,
        Config.QUOTE_REFRESH_MS,
    )

    mm = MarketMaker()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal(sig):
        log.info("Received %s – initiating graceful shutdown", sig.name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    mm_task = asyncio.create_task(mm.run(), name="market-maker")

    # Wait until stop signal or task completion
    done, pending = await asyncio.wait(
        [mm_task, asyncio.create_task(stop_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if not mm_task.done():
        mm_task.cancel()
        try:
            await mm_task
        except asyncio.CancelledError:
            pass

    log.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
