"""
Configuration loader for BTC market maker bot.
All values sourced from environment / .env file.
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
    # Credentials
    API_KEY: str = _get("BINANCE_API_KEY")
    API_SECRET: str = _get("BINANCE_API_SECRET")

    # Market
    SYMBOL: str = _get("SYMBOL", "BTCUSDT")

    # Sizing
    ORDER_SIZE_QUOTE: float = _get("ORDER_SIZE_QUOTE", "50", float)
    MIN_ORDER_SIZE_BTC: float = _get("MIN_ORDER_SIZE_BTC", "0.0001", float)
    MAX_POSITION_USDT: float = _get("MAX_POSITION_USDT", "500", float)

    # Pricing
    SPREAD_BPS: int = _get("SPREAD_BPS", "20", int)
    FEE_RATE_BPS: int = _get("FEE_RATE_BPS", "10", int)
    MAX_SPREAD_DEVIATION_BPS: int = _get("MAX_SPREAD_DEVIATION_BPS", "50", int)

    # Timing (convert ms -> seconds where needed)
    REBALANCE_INTERVAL_SEC: int = _get("REBALANCE_INTERVAL_SEC", "300", int)
    QUOTE_REFRESH_MS: int = _get("QUOTE_REFRESH_MS", "500", int)
    CANCEL_REPLACE_TIMEOUT_MS: int = _get("CANCEL_REPLACE_TIMEOUT_MS", "90", int)

    # WebSocket endpoints
    WS_BASE: str = "wss://stream.binance.com:9443/ws"
    REST_BASE: str = "https://api.binance.com"

    # Derived: total minimum spread = 2x fee to break even + configured base
    @classmethod
    def min_spread_bps(cls) -> int:
        return cls.FEE_RATE_BPS * 2 + cls.SPREAD_BPS
