"""
CSV trade logger for the Polymarket BTC 5-minute market maker.

Creates timestamped CSV files in the `logs/` directory:
  - trades_YYYYMMDD_HHMMSS.csv  — one row per resolved trade
  - session_YYYYMMDD_HHMMSS.csv — one row per session summary (written on exit)

Columns captured per trade:
  timestamp, window_start, side, entry_price, size_usdc, shares,
  p_signal, sigma, vol_regime, obi, volume_ratio, spread,
  btc_open, btc_close, market_ask, market_bid,
  outcome, pnl, fee, balance, exit_type,
  hedge_filled, hedge_side, hedge_price,
  stc_at_fill, elapsed_since_fill
"""
import csv
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

LOGS_DIR = Path(__file__).parent / "logs"


@dataclass
class TradeRow:
    """All data collected for one resolved trade."""
    # Timing
    timestamp: str = ""
    window_start: int = 0
    window_end: int = 0

    # Entry
    side: str = ""                  # YES / NO
    entry_price: float = 0.0
    size_usdc: float = 0.0
    shares: float = 0.0

    # Signal state at entry
    p_signal: float = 0.0
    sigma: float = 0.0
    vol_regime: str = ""
    obi: float = 0.0
    volume_ratio: float = 0.0
    spread: float = 0.0

    # Market state
    btc_open: float = 0.0
    btc_close: float = 0.0
    market_ask: float = 0.0
    market_bid: float = 0.0

    # Outcome
    outcome: str = ""               # WIN / LOSS / STOP-LOSS
    pnl: float = 0.0
    fee: float = 0.0
    balance_after: float = 0.0

    # Exit details
    exit_type: str = ""             # binary / stop-loss / sell-exit
    stc_at_fill: float = 0.0       # seconds to close when filled
    elapsed_in_position: float = 0.0  # seconds held

    # Hedge info
    hedge_filled: bool = False
    hedge_side: str = ""
    hedge_price: float = 0.0


class TradeLogger:
    """Writes trade data to CSV files in logs/ directory."""

    def __init__(self) -> None:
        LOGS_DIR.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._trades_path = LOGS_DIR / f"trades_{ts}.csv"
        self._session_path = LOGS_DIR / f"session_{ts}.csv"
        self._session_start = time.time()
        self._trade_count = 0
        self._header_written = False
        log.info("Trade logger → %s", self._trades_path)

    def log_trade(self, row: TradeRow) -> None:
        """Append one trade row to the CSV file."""
        if not row.timestamp:
            row.timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row.shares = round(row.size_usdc / row.entry_price, 2) if row.entry_price > 0 else 0.0

        d = asdict(row)
        try:
            write_header = not self._header_written
            with open(self._trades_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=d.keys())
                if write_header:
                    writer.writeheader()
                    self._header_written = True
                writer.writerow(d)
            self._trade_count += 1
        except Exception as exc:
            log.error("Failed to write trade CSV: %s", exc)

    def log_session_summary(
        self,
        *,
        total_trades: int,
        wins: int,
        losses: int,
        stop_losses: int,
        total_pnl: float,
        total_fees: float,
        final_balance: float,
        avg_entry_price: float,
        two_sided_fills: int,
    ) -> None:
        """Write session summary CSV on shutdown."""
        uptime_h = (time.time() - self._session_start) / 3600
        summary = {
            "session_start": datetime.fromtimestamp(
                self._session_start, tz=timezone.utc
            ).isoformat(timespec="seconds"),
            "session_end": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "uptime_hours": round(uptime_h, 2),
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "stop_losses": stop_losses,
            "win_rate": round(wins / total_trades * 100, 1) if total_trades else 0.0,
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 4),
            "avg_entry_price": round(avg_entry_price, 4),
            "final_balance": round(final_balance, 2),
            "two_sided_fills": two_sided_fills,
            "avg_pnl_per_trade": round(total_pnl / total_trades, 3) if total_trades else 0.0,
            "trades_csv": str(self._trades_path),
        }
        try:
            with open(self._session_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=summary.keys())
                writer.writeheader()
                writer.writerow(summary)
            log.info("Session summary → %s", self._session_path)
        except Exception as exc:
            log.error("Failed to write session CSV: %s", exc)
