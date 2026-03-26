"""
Configuration loader for Polymarket BTC 5-minute market maker bot.

All parameters are loaded from environment variables with sensible defaults.
Validates constraints on startup to catch misconfiguration early.
"""
import logging
import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


def _get(key: str, default=None, cast=str):
    val = os.getenv(key, default)
    if val is None:
        raise ValueError(f"Missing required env var: {key}")
    return cast(val)


class Config:
    # Wallet
    PRIVATE_KEY: str = _get("POLYMARKET_PRIVATE_KEY")

    # Endpoints
    CLOB_API_URL: str = _get("CLOB_API_URL", "https://clob.polymarket.com")
    CLOB_WS_URL: str = _get("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/")
    BINANCE_WS_URL: str = _get("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws")

    # Market
    BTC_SYMBOL: str = _get("BTC_SYMBOL", "BTCUSDT")

    # Sizing (USDC has 6 decimal places on Polygon)
    ORDER_SIZE_USDC: float = _get("ORDER_SIZE_USDC", "50", float)
    ORDER_SIZE_RAW: int = int(_get("ORDER_SIZE_USDC", "50", float) * 1_000_000)

    # Pricing
    MAX_ENTRY_PRICE: float = _get("MAX_ENTRY_PRICE", "0.95", float)
    MIN_BID_PRICE: float = _get("MIN_BID_PRICE", "0.05", float)

    # Two-sided spread parameters (basis points)
    BASE_SPREAD_BPS: int = _get("BASE_SPREAD_BPS", "300", int)      # 3¢ base
    MIN_SPREAD_BPS: int = _get("MIN_SPREAD_BPS", "150", int)        # 1.5¢ floor (raised for safety)
    MAX_SPREAD_BPS: int = _get("MAX_SPREAD_BPS", "600", int)        # 6¢ ceiling

    # Signal model: BTC 5-minute return volatility.
    SIGMA_5M: float = _get("SIGMA_5M", "0.0022", float)

    # --- Polymarket fee model (exact formula) ---
    _FEE_SWITCH_DATE = datetime(2026, 3, 30, tzinfo=timezone.utc)
    _NEW_FEE_REGIME = datetime.now(timezone.utc) >= _FEE_SWITCH_DATE
    FEE_RATE: float = _get("FEE_RATE",
                            "0.072" if _NEW_FEE_REGIME else "0.25", float)
    FEE_EXPONENT: int = _get("FEE_EXPONENT",
                              "1" if _NEW_FEE_REGIME else "2", int)
    MAKER_REBATE_PCT: float = _get("MAKER_REBATE_PCT", "0.20", float)
    FEE_CACHE_TTL_SEC: float = _get("FEE_CACHE_TTL_SEC", "300", float)

    # Time-weighted entry: skip quoting in the first QUIET_PERIOD_SEC
    QUIET_PERIOD_SEC: int = _get("QUIET_PERIOD_SEC", "35", int)

    # Minimum |p_up - 0.5| before the bot will quote
    MIN_SIGNAL_EDGE: float = _get("MIN_SIGNAL_EDGE", "0.03", float)

    # --- Minimum expected value filter ---
    # Reject trades where EV < this threshold (USDC per trade).
    # Prevents placing orders with marginal or negative expected profit.
    MIN_EV_USDC: float = _get("MIN_EV_USDC", "0.10", float)

    # --- Minimum confidence to quote ---
    # Signal confidence below this → don't place orders.
    MIN_CONFIDENCE: float = _get("MIN_CONFIDENCE", "0.15", float)

    # Kelly sizing
    KELLY_FRACTION: float = _get("KELLY_FRACTION", "1.0", float)
    KELLY_MIN_SIZE_MULT: float = _get("KELLY_MIN_SIZE_MULT", "0.3", float)
    KELLY_MAX_SIZE_MULT: float = _get("KELLY_MAX_SIZE_MULT", "3.0", float)

    # Timing (ms)
    QUOTE_REFRESH_MS: int = _get("QUOTE_REFRESH_MS", "200", int)
    CANCEL_REPLACE_TIMEOUT_MS: int = _get("CANCEL_REPLACE_TIMEOUT_MS", "90", int)
    ENTRY_WINDOW_SEC: int = _get("ENTRY_WINDOW_SEC", "10", int)
    EXIT_WINDOW_SEC: int = _get("EXIT_WINDOW_SEC", "2", int)

    # CLOB polling interval
    ORDERBOOK_POLL_SEC: float = _get("ORDERBOOK_POLL_SEC", "10", float)

    # Risk — session loss limit
    MAX_LOSS_USDC: float = _get("MAX_LOSS_USDC", "200", float)

    # --- Per-window max loss ---
    # Maximum loss allowed per single window (prevents catastrophic single-trade losses)
    MAX_LOSS_PER_WINDOW_USDC: float = _get("MAX_LOSS_PER_WINDOW_USDC", "80", float)

    # --- Drawdown-based size reduction ---
    # When session P&L is negative, reduce position sizes proportionally.
    # At drawdown = DRAWDOWN_FULL_REDUCE_USDC, size is halved.
    DRAWDOWN_SIZE_REDUCTION: bool = _get("DRAWDOWN_SIZE_REDUCTION", "true",
                                          lambda v: v.lower() in ("true", "1", "yes"))
    DRAWDOWN_FULL_REDUCE_USDC: float = _get("DRAWDOWN_FULL_REDUCE_USDC", "100", float)
    DRAWDOWN_MIN_SIZE_MULT: float = _get("DRAWDOWN_MIN_SIZE_MULT", "0.25", float)

    # --- Consecutive loss tracker ---
    # After N consecutive losses, reduce size by this multiplier.
    CONSEC_LOSS_REDUCE_AFTER: int = _get("CONSEC_LOSS_REDUCE_AFTER", "3", int)
    CONSEC_LOSS_SIZE_MULT: float = _get("CONSEC_LOSS_SIZE_MULT", "0.5", float)

    # Stale data guard
    STALE_DATA_MAX_SEC: float = _get("STALE_DATA_MAX_SEC", "5", float)

    # Volatility gate
    VOLATILITY_GATE_BPS: float = _get("VOLATILITY_GATE_BPS", "200", float)

    STATS_LOG_INTERVAL: int = _get("STATS_LOG_INTERVAL", "10", int)

    # --- Order Book Imbalance (OBI) ---
    OBI_WEIGHT: float = _get("OBI_WEIGHT", "0.08", float)

    # --- Mean reversion (Ornstein-Uhlenbeck) ---
    MEAN_REVERSION_KAPPA: float = _get("MEAN_REVERSION_KAPPA", "0.20", float)

    # --- Volume signal ---
    VOLUME_CONFIDENCE_WEIGHT: float = _get("VOLUME_CONFIDENCE_WEIGHT", "0.10", float)

    # --- Multi-timeframe trend filter ---
    TREND_BIAS_WEIGHT: float = _get("TREND_BIAS_WEIGHT", "0.15", float)
    TREND_SENSITIVITY: float = _get("TREND_SENSITIVITY", "0.01", float)
    TREND_WEIGHT_MIN: float = _get("TREND_WEIGHT_MIN", "0.03", float)
    TREND_WEIGHT_MAX: float = _get("TREND_WEIGHT_MAX", "0.15", float)

    # --- Candle close location pattern ---
    CANDLE_PATTERN_WEIGHT: float = _get("CANDLE_PATTERN_WEIGHT", "0.04", float)

    # --- Time-of-day volatility adjustment ---
    TOD_VOL_ADJUST_ENABLED: bool = _get("TOD_VOL_ADJUST_ENABLED", "true",
                                         lambda v: v.lower() in ("true", "1", "yes"))

    # --- Volatility regime adjustments ---
    VOL_REGIME_STORM_SIZE_MULT: float = _get("VOL_REGIME_STORM_SIZE_MULT", "0.5", float)
    VOL_REGIME_STORM_SPREAD_MULT: float = _get("VOL_REGIME_STORM_SPREAD_MULT", "1.5", float)
    VOL_REGIME_CALM_SPREAD_MULT: float = _get("VOL_REGIME_CALM_SPREAD_MULT", "0.85", float)

    # --- Smart hedge timeout ---
    HEDGE_TIMEOUT_SEC: float = _get("HEDGE_TIMEOUT_SEC", "25", float)
    HEDGE_AGGRESSIVE_SPREAD_MULT: float = _get("HEDGE_AGGRESSIVE_SPREAD_MULT", "0.3", float)
    HEDGE_TIMEOUT_FRAC: float = _get("HEDGE_TIMEOUT_FRAC", "0.15", float)
    HEDGE_ONLY_IF_LOSING: bool = _get("HEDGE_ONLY_IF_LOSING", "true",
                                       lambda v: v.lower() in ("true", "1", "yes"))

    # --- Adaptive stop-loss ---
    STOP_LOSS_ENABLED: bool = _get("STOP_LOSS_ENABLED", "true", lambda v: v.lower() in ("true", "1", "yes"))
    STOP_LOSS_THRESHOLD: float = _get("STOP_LOSS_THRESHOLD", "0.12", float)
    STOP_LOSS_BASE: float = _get("STOP_LOSS_BASE", "0.12", float)
    STOP_LOSS_VOL_SCALE: float = _get("STOP_LOSS_VOL_SCALE", "0.04", float)
    STOP_LOSS_MIN_STC: float = _get("STOP_LOSS_MIN_STC", "15", float)

    # --- Sell-side exit ---
    SELL_EXIT_ENABLED: bool = _get("SELL_EXIT_ENABLED", "true", lambda v: v.lower() in ("true", "1", "yes"))

    # Market constants
    MARKET_WINDOW_SEC: int = 300
    MARKETS_PER_DAY: int = 288

    # Paper trading
    PAPER_TRADING: bool = _get("PAPER_TRADING", "true", lambda v: v.lower() in ("true", "1", "yes"))
    PAPER_BALANCE_USDC: float = _get("PAPER_BALANCE_USDC", "1000", float)

    # --- Paper trading realism ---
    PAPER_SLIPPAGE_BPS: float = _get("PAPER_SLIPPAGE_BPS", "10", float)  # 0.1% random slippage
    PAPER_LATENCY_MS: float = _get("PAPER_LATENCY_MS", "50", float)     # simulated 50ms latency
    PAPER_PARTIAL_FILL_ENABLED: bool = _get("PAPER_PARTIAL_FILL_ENABLED", "true",
                                             lambda v: v.lower() in ("true", "1", "yes"))

    # Polygon chain ID (Polymarket runs on Polygon PoS)
    CHAIN_ID: int = 137

    # CLOB contract addresses (mainnet)
    EXCHANGE_ADDRESS: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    CONDITIONAL_TOKEN_ADDRESS: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    USDC_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

    @classmethod
    def validate(cls) -> List[str]:
        """
        Validate configuration constraints. Returns list of error messages.
        Called on startup to catch misconfiguration before trading.
        """
        errors = []

        if cls.ORDER_SIZE_USDC <= 0:
            errors.append(f"ORDER_SIZE_USDC must be positive, got {cls.ORDER_SIZE_USDC}")
        if cls.MAX_ENTRY_PRICE <= 0.5 or cls.MAX_ENTRY_PRICE > 0.99:
            errors.append(f"MAX_ENTRY_PRICE must be in (0.5, 0.99], got {cls.MAX_ENTRY_PRICE}")
        if cls.MIN_BID_PRICE < 0.01 or cls.MIN_BID_PRICE >= cls.MAX_ENTRY_PRICE:
            errors.append(f"MIN_BID_PRICE must be in [0.01, MAX_ENTRY_PRICE), got {cls.MIN_BID_PRICE}")
        if cls.MIN_SPREAD_BPS <= 0:
            errors.append(f"MIN_SPREAD_BPS must be positive, got {cls.MIN_SPREAD_BPS}")
        if cls.MIN_SPREAD_BPS >= cls.MAX_SPREAD_BPS:
            errors.append(f"MIN_SPREAD_BPS ({cls.MIN_SPREAD_BPS}) must be < MAX_SPREAD_BPS ({cls.MAX_SPREAD_BPS})")
        if cls.SIGMA_5M <= 0:
            errors.append(f"SIGMA_5M must be positive, got {cls.SIGMA_5M}")
        if cls.KELLY_FRACTION < 0 or cls.KELLY_FRACTION > 2:
            errors.append(f"KELLY_FRACTION must be in [0, 2], got {cls.KELLY_FRACTION}")
        if cls.MAX_LOSS_USDC <= 0:
            errors.append(f"MAX_LOSS_USDC must be positive, got {cls.MAX_LOSS_USDC}")
        if cls.QUIET_PERIOD_SEC < 0 or cls.QUIET_PERIOD_SEC > cls.MARKET_WINDOW_SEC:
            errors.append(f"QUIET_PERIOD_SEC must be in [0, {cls.MARKET_WINDOW_SEC}], got {cls.QUIET_PERIOD_SEC}")
        if cls.EXIT_WINDOW_SEC < 1 or cls.EXIT_WINDOW_SEC > 30:
            errors.append(f"EXIT_WINDOW_SEC must be in [1, 30], got {cls.EXIT_WINDOW_SEC}")
        if cls.MIN_CONFIDENCE < 0 or cls.MIN_CONFIDENCE > 1:
            errors.append(f"MIN_CONFIDENCE must be in [0, 1], got {cls.MIN_CONFIDENCE}")
        if cls.FEE_RATE < 0 or cls.FEE_RATE > 1:
            errors.append(f"FEE_RATE must be in [0, 1], got {cls.FEE_RATE}")
        if cls.PAPER_BALANCE_USDC <= 0:
            errors.append(f"PAPER_BALANCE_USDC must be positive, got {cls.PAPER_BALANCE_USDC}")

        return errors


# Need this import for validate() type hint
from typing import List
