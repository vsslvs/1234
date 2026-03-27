"""
Configuration loader for Polymarket BTC 5-minute market maker bot.

Validates all parameters at import time — fail fast on bad config.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default=None, cast=str):
    val = os.getenv(key, default)
    if val is None:
        raise ValueError(f"Missing required env var: {key}")
    return cast(val)


def _get_bool(key: str, default: str = "false") -> bool:
    return _get(key, default).lower() in ("true", "1", "yes")


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
    MAX_OPEN_ORDERS: int = _get("MAX_OPEN_ORDERS", "4", int)

    # Pricing
    TARGET_PRICE_YES: float = _get("TARGET_PRICE_YES", "0.92", float)
    TARGET_PRICE_NO: float = _get("TARGET_PRICE_NO", "0.92", float)
    MIN_EDGE_BPS: int = _get("MIN_EDGE_BPS", "50", int)

    # Timing (ms)
    QUOTE_REFRESH_MS: int = _get("QUOTE_REFRESH_MS", "200", int)
    CANCEL_REPLACE_TIMEOUT_MS: int = _get("CANCEL_REPLACE_TIMEOUT_MS", "90", int)
    ENTRY_WINDOW_SEC: int = _get("ENTRY_WINDOW_SEC", "10", int)
    EXIT_WINDOW_SEC: int = _get("EXIT_WINDOW_SEC", "2", int)

    # Risk — exposure
    MAX_EXPOSURE_USDC: float = _get("MAX_EXPOSURE_USDC", "500", float)

    # Risk — drawdown and loss limits
    MAX_DRAWDOWN_USDC: float = _get("MAX_DRAWDOWN_USDC", "100", float)
    MAX_DAILY_LOSS_USDC: float = _get("MAX_DAILY_LOSS_USDC", "200", float)
    MAX_CONSECUTIVE_LOSSES: int = _get("MAX_CONSECUTIVE_LOSSES", "5", int)
    CIRCUIT_BREAKER_COOLDOWN_SEC: int = _get("CIRCUIT_BREAKER_COOLDOWN_SEC", "600", int)

    # Risk — Kelly sizing
    KELLY_ENABLED: bool = _get_bool("KELLY_ENABLED", "true")
    KELLY_FRACTION: float = _get("KELLY_FRACTION", "0.25", float)

    # Volatility gate: skip trading if the latest 5-minute Binance candle
    # high-low range exceeds this threshold (basis points, relative to close).
    # 200 bps = 2% range.  At σ₅ = 0.22% typical, a 2% range represents
    # ~3× normal volatility and signals flash-crash / news event conditions
    # where the random-walk model breaks down and EV turns negative.
    VOLATILITY_GATE_BPS: float = _get("VOLATILITY_GATE_BPS", "200", float)

    # Log a statistics summary every N window rollovers (≈ every N × 5 minutes).
    STATS_LOG_INTERVAL: int = _get("STATS_LOG_INTERVAL", "10", int)

    # 5-minute BTC markets: 288 per day, each window = 300 seconds
    MARKET_WINDOW_SEC: int = 300
    MARKETS_PER_DAY: int = 288

    # Dashboard
    DASHBOARD_PORT: int = _get("DASHBOARD_PORT", "8080", int)
    DASHBOARD_HOST: str = _get("DASHBOARD_HOST", "0.0.0.0")
    DASHBOARD_PASSWORD: str = _get("DASHBOARD_PASSWORD", "")

    # Polygon chain ID (Polymarket runs on Polygon PoS)
    CHAIN_ID: int = 137

    # CLOB contract addresses (mainnet)
    EXCHANGE_ADDRESS: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    CONDITIONAL_TOKEN_ADDRESS: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    USDC_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon

    # Paper trading mode
    PAPER_MODE: bool = _get_bool("PAPER_MODE", "false")

    # Logging format: "text" or "json"
    LOG_FORMAT: str = _get("LOG_FORMAT", "text")

    # Signal weights
    OFI_WEIGHT: float = _get("OFI_WEIGHT", "0.15", float)

    # Staleness threshold (ms) — skip quoting if price data older than this
    STALENESS_THRESHOLD_MS: int = _get("STALENESS_THRESHOLD_MS", "2000", int)


# ---------------------------------------------------------------------------
# Startup validation — fail fast on obviously invalid config
# ---------------------------------------------------------------------------

def _validate() -> None:
    errors = []

    if not Config.PRIVATE_KEY or Config.PRIVATE_KEY == "0xyour_private_key_here":
        errors.append("POLYMARKET_PRIVATE_KEY is not set")

    if not 0 < Config.TARGET_PRICE_YES < 1:
        errors.append(f"TARGET_PRICE_YES={Config.TARGET_PRICE_YES} must be in (0, 1)")
    if not 0 < Config.TARGET_PRICE_NO < 1:
        errors.append(f"TARGET_PRICE_NO={Config.TARGET_PRICE_NO} must be in (0, 1)")

    if Config.ORDER_SIZE_USDC <= 0:
        errors.append(f"ORDER_SIZE_USDC={Config.ORDER_SIZE_USDC} must be > 0")
    if Config.ORDER_SIZE_USDC > Config.MAX_EXPOSURE_USDC:
        errors.append(
            f"ORDER_SIZE_USDC={Config.ORDER_SIZE_USDC} exceeds "
            f"MAX_EXPOSURE_USDC={Config.MAX_EXPOSURE_USDC}"
        )

    if Config.ENTRY_WINDOW_SEC <= Config.EXIT_WINDOW_SEC:
        errors.append(
            f"ENTRY_WINDOW_SEC={Config.ENTRY_WINDOW_SEC} must be > "
            f"EXIT_WINDOW_SEC={Config.EXIT_WINDOW_SEC}"
        )

    if Config.MAX_EXPOSURE_USDC <= 0:
        errors.append(f"MAX_EXPOSURE_USDC={Config.MAX_EXPOSURE_USDC} must be > 0")

    if Config.MAX_DRAWDOWN_USDC <= 0:
        errors.append(f"MAX_DRAWDOWN_USDC={Config.MAX_DRAWDOWN_USDC} must be > 0")

    if not 0 < Config.KELLY_FRACTION <= 1:
        errors.append(f"KELLY_FRACTION={Config.KELLY_FRACTION} must be in (0, 1]")

    if not 50 <= Config.VOLATILITY_GATE_BPS <= 2000:
        errors.append(f"VOLATILITY_GATE_BPS={Config.VOLATILITY_GATE_BPS} must be in [50, 2000]")

    if Config.LOG_FORMAT not in ("text", "json"):
        errors.append(f"LOG_FORMAT={Config.LOG_FORMAT} must be 'text' or 'json'")

    if errors:
        for e in errors:
            print(f"CONFIG ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)


_validate()
