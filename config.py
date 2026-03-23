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
    MAX_OPEN_ORDERS: int = _get("MAX_OPEN_ORDERS", "4", int)

    # Pricing
    # Dynamic entry: use market ask, capped at MAX_ENTRY_PRICE.
    # Old fixed TARGET_PRICE_YES/NO=0.92 replaced with dynamic pricing.
    MAX_ENTRY_PRICE: float = _get("MAX_ENTRY_PRICE", "0.95", float)
    MIN_EDGE_BPS: int = _get("MIN_EDGE_BPS", "200", int)
    # p_signal must exceed break-even (= entry_price) by this many bps
    BREAKEVEN_SAFETY_BPS: int = _get("BREAKEVEN_SAFETY_BPS", "200", int)

    # Timing (ms)
    QUOTE_REFRESH_MS: int = _get("QUOTE_REFRESH_MS", "200", int)
    CANCEL_REPLACE_TIMEOUT_MS: int = _get("CANCEL_REPLACE_TIMEOUT_MS", "90", int)
    ENTRY_WINDOW_SEC: int = _get("ENTRY_WINDOW_SEC", "10", int)
    EXIT_WINDOW_SEC: int = _get("EXIT_WINDOW_SEC", "2", int)

    # Risk
    MAX_EXPOSURE_USDC: float = _get("MAX_EXPOSURE_USDC", "500", float)

    # Stale data guard: skip trading if the Binance order book hasn't been
    # updated for more than this many seconds.  Protects against placing
    # orders based on outdated BTC prices when the WebSocket is disconnected
    # but the Polymarket API is still reachable.
    STALE_DATA_MAX_SEC: float = _get("STALE_DATA_MAX_SEC", "5", float)

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

    # Paper trading (dry run with virtual balance, no real orders)
    PAPER_TRADING: bool = _get("PAPER_TRADING", "true", lambda v: v.lower() in ("true", "1", "yes"))
    PAPER_BALANCE_USDC: float = _get("PAPER_BALANCE_USDC", "1000", float)

    # Polygon chain ID (Polymarket runs on Polygon PoS)
    CHAIN_ID: int = 137

    # CLOB contract addresses (mainnet)
    EXCHANGE_ADDRESS: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    CONDITIONAL_TOKEN_ADDRESS: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    USDC_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon
