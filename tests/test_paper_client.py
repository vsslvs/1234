"""Tests for paper trading client."""
import pytest
import asyncio

from paper_trading import PaperClient


@pytest.mark.asyncio
async def test_place_and_cancel():
    """Place an order and cancel it."""
    client = PaperClient()
    client._session = None  # no real HTTP needed for paper

    order = await client.place_maker_order(
        token_id="test-token", side=0, price=0.92, size_usdc=50.0,
    )
    assert order.order_id.startswith("paper-")
    assert order.price == 0.92
    assert order.size_usdc == 50.0
    assert len(client._open_orders) == 1

    ok = await client.cancel_order(order.order_id)
    assert ok
    assert len(client._open_orders) == 0


@pytest.mark.asyncio
async def test_cancel_replace():
    """Cancel/replace should remove old and create new."""
    client = PaperClient()
    client._session = None

    old = await client.place_maker_order(
        token_id="test-token", side=0, price=0.92, size_usdc=50.0,
    )
    new = await client.cancel_replace(old, new_price=0.93)
    assert new is not None
    assert new.price == 0.93
    assert new.order_id != old.order_id
    assert old.order_id not in client._open_orders


@pytest.mark.asyncio
async def test_cancel_all():
    """Cancel all should clear all orders."""
    client = PaperClient()
    client._session = None

    for i in range(5):
        await client.place_maker_order(
            token_id=f"token-{i}", side=0, price=0.92, size_usdc=50.0,
        )
    assert len(client._open_orders) == 5

    await client.cancel_all_orders()
    assert len(client._open_orders) == 0


@pytest.mark.asyncio
async def test_get_open_orders_filtered():
    """get_open_orders with token filter."""
    client = PaperClient()
    client._session = None

    await client.place_maker_order("token-a", 0, 0.92, 50.0)
    await client.place_maker_order("token-b", 0, 0.92, 50.0)
    await client.place_maker_order("token-a", 0, 0.93, 30.0)

    all_orders = await client.get_open_orders()
    assert len(all_orders) == 3

    filtered = await client.get_open_orders("token-a")
    assert len(filtered) == 2


@pytest.mark.asyncio
async def test_check_approvals_noop():
    """check_approvals should be a no-op in paper mode."""
    client = PaperClient()
    client._session = None
    await client.check_approvals()  # should not raise


@pytest.mark.asyncio
async def test_summary():
    """Summary should reflect operations."""
    client = PaperClient()
    client._session = None

    await client.place_maker_order("t", 0, 0.92, 50.0)
    order = await client.place_maker_order("t", 0, 0.93, 50.0)
    await client.cancel_order(order.order_id)

    s = client.summary()
    assert s["total_placed"] == 2
    assert s["total_cancelled"] == 1
    assert s["open_orders"] == 1
