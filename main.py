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
import argparse
import asyncio
import logging
import os
import signal
import sys
import termios
import tty

from bot_state import state as dashboard_state
from config import Config
from dashboard import start_dashboard, set_market_maker
from market_calculator import MarketCalculator
from market_maker import MarketMaker
from paper_client import PaperClient
from polymarket_client import PolymarketClient
from ws_orderbook import OrderBookWS


def _setup_logging() -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s – %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _parse_args():
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m market maker")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--paper", action="store_true", help="Start in paper trading mode")
    group.add_argument("--live", action="store_true", help="Start in live trading mode")
    return parser.parse_args()


async def _stdin_listener(mm, live_client, paper_client, log):
    """Listen for keyboard commands in the terminal."""
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        log.info("Keyboard shortcuts: [p] toggle paper/live  [q] quit")
        while True:
            char = await loop.run_in_executor(None, sys.stdin.read, 1)
            if char == 'p':
                if dashboard_state.paper_trading:
                    await mm.swap_client(live_client)
                    log.info("Switched to LIVE mode (keyboard)")
                else:
                    await mm.swap_client(paper_client)
                    log.info("Switched to PAPER mode (keyboard)")
            elif char == 'q':
                log.info("Quit requested (keyboard)")
                break
    except asyncio.CancelledError:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


async def _main() -> None:
    args = _parse_args()
    _setup_logging()
    log = logging.getLogger("main")
    wallet_short = Config.PRIVATE_KEY[:6] + "..." + Config.PRIVATE_KEY[-4:]

    # Determine starting mode: CLI flag > .env setting
    if args.live:
        start_paper = False
    elif args.paper:
        start_paper = True
    else:
        start_paper = Config.PAPER_TRADING

    mode = "PAPER" if start_paper else "LIVE"
    log.info("BTC 5m market maker starting | %s mode | wallet=%s", mode, wallet_short)
    dashboard_state.wallet = wallet_short
    dashboard_state.paper_trading = start_paper

    # Web dashboard
    dash_runner = await start_dashboard()

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

    paper_client = PaperClient()
    live_client = PolymarketClient()

    # Start with the determined mode
    start_client = paper_client if start_paper else live_client

    async with paper_client:
        async with live_client:
            async with MarketCalculator(ob_ws) as calc:
                mm = MarketMaker(start_client, calc, ob_ws)
                set_market_maker(mm, live_client, paper_client)

                loop = asyncio.get_running_loop()
                stop_event = asyncio.Event()

                def _handle_signal(sig):
                    log.info("Signal %s received — stopping", sig.name)
                    stop_event.set()

                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, _handle_signal, sig)

                mm_task = asyncio.create_task(mm.run(), name="market-maker")
                stdin_task = asyncio.create_task(
                    _stdin_listener(mm, live_client, paper_client, log),
                    name="stdin-listener",
                )

                await asyncio.wait(
                    [mm_task, asyncio.create_task(stop_event.wait()), stdin_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                log.info("Stopping — cancelling all open orders")
                await mm.stop()
                mm_task.cancel()
                ws_task.cancel()
                stdin_task.cancel()
                try:
                    await asyncio.gather(mm_task, ws_task, stdin_task, return_exceptions=True)
                except asyncio.CancelledError:
                    pass

    await dash_runner.cleanup()
    log.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
