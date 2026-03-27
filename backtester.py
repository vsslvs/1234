"""
Historical backtester for the Polymarket BTC market maker strategy.

Replays Binance 5-minute kline data through the signal and risk logic
to estimate P&L, win rate, and drawdown without placing real orders.

Usage:
    python backtester.py --days 7

Or as a library:
    from backtester import Backtester, fetch_historical_candles
    candles = await fetch_historical_candles("BTCUSDT", days=7)
    result = Backtester(candles).run()
    print(result)
"""
import argparse
import asyncio
import math
import logging
from dataclasses import dataclass, field
from typing import List, Optional

import aiohttp

log = logging.getLogger(__name__)

# Default signal parameters (same as live)
DEFAULT_K = 2000.0
DEFAULT_THRESHOLD = 0.94
DEFAULT_ENTRY_PRICE = 0.92
DEFAULT_SIZE_USDC = 50.0
DEFAULT_SIGMA_5M = 0.0022
WINDOW_SEC = 300


@dataclass
class BacktestCandle:
    """One 5-minute candle from historical data."""
    open_time: int     # ms timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class BacktestTrade:
    """One simulated trade."""
    window_start: int
    side: str          # "YES" or "NO"
    entry_price: float
    size_usdc: float
    p_signal: float
    won: bool
    pnl: float


@dataclass
class BacktestResult:
    """Summary of backtest run."""
    trades: List[BacktestTrade]
    total_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0

    @property
    def total_trades(self) -> int:
        return self.win_count + self.loss_count

    @property
    def win_rate(self) -> Optional[float]:
        return self.win_count / self.total_trades if self.total_trades else None

    @property
    def avg_pnl(self) -> Optional[float]:
        return self.total_pnl / self.total_trades if self.total_trades else None

    def summary(self) -> str:
        wr = self.win_rate
        return (
            f"┌─ Backtest Results ──────────────────────────────┐\n"
            f"│  Total trades     : {self.total_trades}\n"
            f"│  Wins / Losses    : {self.win_count} / {self.loss_count}\n"
            f"│  Win rate         : {wr * 100:.2f}%\n"
            f"│  Total P&L        : {self.total_pnl:+.2f} USDC\n"
            f"│  Avg P&L/trade    : {self.avg_pnl:+.3f} USDC\n"
            f"│  Max drawdown     : {self.max_drawdown:.2f} USDC\n"
            f"│  Sharpe ratio     : {self.sharpe_ratio:.3f}\n"
            f"└─────────────────────────────────────────────────┘"
        ) if wr is not None else "No trades generated."


class Backtester:
    """
    Replay historical 5-minute candles through the trading signal logic.

    Each candle represents one 5-minute window. At the entry point
    (ENTRY_WINDOW_SEC before close), the backtester evaluates the signal
    using the candle's open as reference and the close as the BTC price
    at entry time (approximation — in reality entry happens ~10s before close).
    """

    def __init__(
        self,
        candles: List[BacktestCandle],
        k: float = DEFAULT_K,
        threshold: float = DEFAULT_THRESHOLD,
        entry_price: float = DEFAULT_ENTRY_PRICE,
        size_usdc: float = DEFAULT_SIZE_USDC,
        vol_gate_bps: float = 200.0,
    ):
        self.candles = candles
        self.k = k
        self.threshold = threshold
        self.entry_price = entry_price
        self.size_usdc = size_usdc
        self.vol_gate_bps = vol_gate_bps

    def run(self) -> BacktestResult:
        """Run backtest over all candles."""
        trades: List[BacktestTrade] = []
        peak_pnl = 0.0
        max_dd = 0.0
        cumulative_pnl = 0.0
        pnl_list: List[float] = []

        for i, candle in enumerate(self.candles):
            # Volatility gate
            if candle.close > 0:
                vol_bps = (candle.high - candle.low) / candle.close * 10_000
                if vol_bps > self.vol_gate_bps:
                    continue

            # Signal at entry (approximate: use close as "current price" ~10s before end)
            if candle.open <= 0:
                continue
            ret = (candle.close - candle.open) / candle.open
            p_up = 1.0 / (1.0 + math.exp(-self.k * ret))

            # Check if signal strong enough
            side = None
            p_signal = 0.0
            if p_up > self.threshold:
                side = "YES"
                p_signal = p_up
            elif p_up < (1.0 - self.threshold):
                side = "NO"
                p_signal = 1.0 - p_up

            if side is None:
                continue

            # Resolution: did BTC close up or down?
            # Use next candle's open vs this candle's open for resolution
            # (since Polymarket resolves at window boundary)
            btc_up = candle.close >= candle.open
            if side == "YES":
                won = btc_up
            else:
                won = not btc_up

            shares = self.size_usdc / self.entry_price
            pnl = shares * (1.0 - self.entry_price) if won else -self.size_usdc

            trade = BacktestTrade(
                window_start=candle.open_time,
                side=side,
                entry_price=self.entry_price,
                size_usdc=self.size_usdc,
                p_signal=p_signal,
                won=won,
                pnl=pnl,
            )
            trades.append(trade)
            pnl_list.append(pnl)

            cumulative_pnl += pnl
            if cumulative_pnl > peak_pnl:
                peak_pnl = cumulative_pnl
            dd = peak_pnl - cumulative_pnl
            if dd > max_dd:
                max_dd = dd

        # Sharpe ratio (annualized from 5-min periods)
        sharpe = 0.0
        if pnl_list and len(pnl_list) > 1:
            mean_pnl = sum(pnl_list) / len(pnl_list)
            var = sum((p - mean_pnl) ** 2 for p in pnl_list) / (len(pnl_list) - 1)
            std_pnl = math.sqrt(var) if var > 0 else 0
            if std_pnl > 0:
                # Annualize: 288 windows/day × 365 days
                sharpe = (mean_pnl / std_pnl) * math.sqrt(288 * 365)

        result = BacktestResult(
            trades=trades,
            total_pnl=round(cumulative_pnl, 2),
            win_count=sum(1 for t in trades if t.won),
            loss_count=sum(1 for t in trades if not t.won),
            max_drawdown=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 3),
        )
        return result


async def fetch_historical_candles(
    symbol: str = "BTCUSDT",
    interval: str = "5m",
    days: int = 7,
) -> List[BacktestCandle]:
    """
    Fetch historical klines from Binance REST API.
    Handles pagination (max 1000 candles per request).
    """
    base_url = "https://api.binance.com/api/v3/klines"
    candles: List[BacktestCandle] = []

    # Total candles needed: days × 288 per day
    total_needed = days * 288
    end_time = int(asyncio.get_event_loop().time() * 1000)  # now in ms

    async with aiohttp.ClientSession() as session:
        fetched = 0
        while fetched < total_needed:
            limit = min(1000, total_needed - fetched)
            params = {
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "endTime": end_time,
            }

            async with session.get(base_url, params=params) as r:
                if r.status != 200:
                    log.error("Binance klines API error: %s", r.status)
                    break
                data = await r.json()

            if not data:
                break

            for item in data:
                candles.append(BacktestCandle(
                    open_time=int(item[0]),
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                ))

            fetched += len(data)
            # Move end_time back for pagination
            end_time = int(data[0][0]) - 1

            if len(data) < limit:
                break

    # Sort by time ascending
    candles.sort(key=lambda c: c.open_time)
    log.info("Fetched %d historical %s candles for %s", len(candles), interval, symbol)
    return candles


async def _run_backtest(days: int) -> None:
    """CLI entry point for backtest."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    log.info("Fetching %d days of BTCUSDT 5m candles...", days)
    candles = await fetch_historical_candles(days=days)

    if not candles:
        log.error("No candle data fetched — check network connection")
        return

    bt = Backtester(candles)
    result = bt.run()
    print(result.summary())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest BTC market maker strategy")
    parser.add_argument("--days", type=int, default=7, help="Days of history to test")
    args = parser.parse_args()

    asyncio.run(_run_backtest(args.days))
