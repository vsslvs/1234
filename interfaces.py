"""
Protocol definitions for dependency injection and testability.

Using typing.Protocol for structural subtyping — implementations
don't need to explicitly inherit, just match the method signatures.
"""
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass
class MakerOrderInfo:
    """Minimal order info shared across live and paper implementations."""
    order_id: str
    token_id: str
    side: int
    price: float
    size_usdc: float


@runtime_checkable
class OrderExecutor(Protocol):
    """Interface for order placement — live or paper."""

    async def place_maker_order(
        self, token_id: str, side: int, price: float, size_usdc: float,
    ) -> "MakerOrderInfo": ...

    async def cancel_order(self, order_id: str) -> bool: ...

    async def cancel_replace(
        self,
        old_order: "MakerOrderInfo",
        new_price: float,
        new_size_usdc: Optional[float] = None,
    ) -> Optional["MakerOrderInfo"]: ...

    async def get_open_orders(self, token_id: Optional[str] = None) -> list: ...

    async def cancel_all_orders(self) -> None: ...

    async def get_fee_rate(self, token_id: str) -> int: ...

    async def check_approvals(self) -> None: ...


@runtime_checkable
class PriceFeed(Protocol):
    """Interface for price data source."""

    @property
    def mid_price(self) -> Optional[float]: ...

    @property
    def best_bid(self) -> Optional[float]: ...

    @property
    def best_ask(self) -> Optional[float]: ...

    @property
    def bid_volume(self) -> float: ...

    @property
    def ask_volume(self) -> float: ...

    @property
    def last_update_ms(self) -> int: ...


@runtime_checkable
class RiskGate(Protocol):
    """Interface for pre-trade risk checks."""

    def can_trade(self, side: str, size_usdc: float, price: float) -> tuple[bool, str]: ...

    def record_fill(self, side: str, size_usdc: float, price: float) -> None: ...

    def record_resolution(self, won: bool, pnl: float) -> None: ...

    def adjusted_size(self, base_size: float, p_signal: float, entry_price: float) -> float: ...

    def to_dict(self) -> dict: ...
