"""
Configuration loader for Polymarket BTC 5-minute market maker bot.
"""
import os
from dotenv import load_dotenv

load_dotenv()


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

    # Two-sided spread parameters (basis points)
    # We BUY at (fair - spread) on both YES and NO.
    # Wider early in window → narrower near close.
    BASE_SPREAD_BPS: int = _get("BASE_SPREAD_BPS", "300", int)      # 3¢ base
    MIN_SPREAD_BPS: int = _get("MIN_SPREAD_BPS", "100", int)        # 1¢ floor
    MAX_SPREAD_BPS: int = _get("MAX_SPREAD_BPS", "600", int)        # 6¢ ceiling

    # Signal model: BTC 5-minute return volatility.
    # 60% annualised → σ₅ = 60% / √(252 × 24 × 12) ≈ 0.22%.
    SIGMA_5M: float = _get("SIGMA_5M", "0.0022", float)

    # Estimated Polymarket fee in price units.  Used to widen the spread
    # in live mode so the fee doesn't eat into our edge.  Paper mode
    # ignores this (fees = 0).  Typical maker fee is ~1-2%.
    LIVE_FEE_ESTIMATE: float = _get("LIVE_FEE_ESTIMATE", "0.015", float)

    # Time-weighted entry: skip quoting in the first QUIET_PERIOD_SEC of
    # each window.  Early in the window the signal is ≈ 50/50 and spread
    # is at its widest → fills are unlikely and carry high adverse selection.
    QUIET_PERIOD_SEC: int = _get("QUIET_PERIOD_SEC", "60", int)

    # Minimum |p_up - 0.5| before the bot will quote.  Prevents placing
    # orders when the signal is essentially a coin flip.
    MIN_SIGNAL_EDGE: float = _get("MIN_SIGNAL_EDGE", "0.03", float)

    # Kelly sizing: half-Kelly fraction applied to ORDER_SIZE_USDC.
    # 0.0 = disabled (always use ORDER_SIZE_USDC).  1.0 = full half-Kelly.
    KELLY_FRACTION: float = _get("KELLY_FRACTION", "1.0", float)
    # Kelly floor/ceiling as multipliers of ORDER_SIZE_USDC.
    KELLY_MIN_SIZE_MULT: float = _get("KELLY_MIN_SIZE_MULT", "0.3", float)
    KELLY_MAX_SIZE_MULT: float = _get("KELLY_MAX_SIZE_MULT", "3.0", float)

    # Timing (ms)
    QUOTE_REFRESH_MS: int = _get("QUOTE_REFRESH_MS", "200", int)
    CANCEL_REPLACE_TIMEOUT_MS: int = _get("CANCEL_REPLACE_TIMEOUT_MS", "90", int)
    ENTRY_WINDOW_SEC: int = _get("ENTRY_WINDOW_SEC", "10", int)  # kept for setup.py compat
    EXIT_WINDOW_SEC: int = _get("EXIT_WINDOW_SEC", "2", int)

    # How often to poll Polymarket CLOB for best bid/ask (seconds).
    # Used for dashboard display and paper-mode fill simulation.
    ORDERBOOK_POLL_SEC: float = _get("ORDERBOOK_POLL_SEC", "10", float)

    # Risk
    # Session loss limit: stop quoting if cumulative P&L drops below this.
    MAX_LOSS_USDC: float = _get("MAX_LOSS_USDC", "200", float)

    # Stale data guard
    STALE_DATA_MAX_SEC: float = _get("STALE_DATA_MAX_SEC", "5", float)

    # Volatility gate (bps): skip trading when 5m candle range exceeds this.
    VOLATILITY_GATE_BPS: float = _get("VOLATILITY_GATE_BPS", "200", float)

    # Log a statistics summary every N window rollovers.
    STATS_LOG_INTERVAL: int = _get("STATS_LOG_INTERVAL", "10", int)

    # 5-minute BTC markets: 288 per day, each window = 300 seconds
    MARKET_WINDOW_SEC: int = 300
    MARKETS_PER_DAY: int = 288

    # Paper trading (dry run with virtual balance, no real orders)
    PAPER_TRADING: bool = _get("PAPER_TRADING", "true", lambda v: v.lower() in ("true", "1", "yes"))
    PAPER_BALANCE_USDC: float = _get("PAPER_BALANCE_USDC", "1000", float)

    # Polygon chain ID (Polymarket runs on Polygon PoS)
    CHAIN_ID: int = 137

    # CLOB contract addresses (mainnet)
    EXCHANGE_ADDRESS: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    CONDITIONAL_TOKEN_ADDRESS: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    USDC_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon
