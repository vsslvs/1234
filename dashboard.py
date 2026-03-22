"""
Web dashboard for the Polymarket BTC market maker bot.

Provides a real-time single-page dashboard via aiohttp + WebSocket.
All bot actions, errors, markets, and statistics are streamed to
connected browser clients.

Usage:
    event_bus = EventBus()
    runner = await start_dashboard(event_bus)
    # ... bot runs, pushes events to event_bus ...
    await runner.cleanup()
"""
import asyncio
import base64
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web

from config import Config

log = logging.getLogger(__name__)

# Maximum log entries kept in memory for new clients
_LOG_BUFFER_SIZE = 200
# Maximum queue depth per WebSocket client before dropping messages
_CLIENT_QUEUE_SIZE = 100


class EventBus:
    """
    Fan-out event bus: bot components push events, WebSocket clients receive them.

    Thread-safe for the single asyncio event loop. Never blocks the caller —
    uses put_nowait with overflow protection.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._log_buffer: deque[dict] = deque(maxlen=_LOG_BUFFER_SIZE)
        self._state: dict = {}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_CLIENT_QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def push(self, event: dict) -> None:
        """Push an event to all subscribers. Never blocks."""
        if event.get("type") == "state":
            self._state = event
        else:
            self._log_buffer.append(event)

        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest and retry
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def push_state(self, data: dict) -> None:
        self.push({"type": "state", "data": data})

    def push_trade(self, data: dict) -> None:
        self.push({"type": "trade", "data": data})

    @property
    def last_state(self) -> dict:
        return self._state

    @property
    def buffered_logs(self) -> list[dict]:
        return list(self._log_buffer)


class DashboardLogHandler(logging.Handler):
    """
    Logging handler that captures log records and pushes them to the EventBus.

    Attach to the root logger so all bot modules' logs are captured.
    Minimal overhead: builds a small dict and calls put_nowait.
    """

    def __init__(self, event_bus: EventBus) -> None:
        super().__init__()
        self._bus = event_bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = {
                "type": "error" if record.levelno >= logging.WARNING else "log",
                "level": record.levelname,
                "module": record.name,
                "message": self.format(record),
                "ts": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).isoformat(),
            }
            self._bus.push(event)
        except Exception:
            pass  # never break the bot


# ---------------------------------------------------------------------------
# aiohttp handlers
# ---------------------------------------------------------------------------

def _check_auth(request: web.Request) -> bool:
    """Return True if request is authorized (or no password is set)."""
    password = Config.DASHBOARD_PASSWORD
    if not password:
        return True
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        # Accept any username, check password only
        _, _, pwd = decoded.partition(":")
        return pwd == password
    except Exception:
        return False


def _auth_response() -> web.Response:
    return web.Response(
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Bot Dashboard"'},
        text="Unauthorized",
    )


async def _handle_index(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return _auth_response()
    html_path = Path(__file__).parent / "dashboard.html"
    return web.FileResponse(html_path)


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    if not _check_auth(request):
        return _auth_response()

    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)

    bus: EventBus = request.app["event_bus"]
    q = bus.subscribe()

    try:
        # Send current state snapshot
        if bus.last_state:
            await ws.send_json(bus.last_state)

        # Send buffered logs so new clients see recent history
        for entry in bus.buffered_logs:
            await ws.send_json(entry)

        # Stream events
        while not ws.closed:
            try:
                event = await asyncio.wait_for(q.get(), timeout=5.0)
                await ws.send_json(event)
            except asyncio.TimeoutError:
                # Send ping to keep alive (handled by heartbeat, but
                # this ensures we check ws.closed periodically)
                continue
            except ConnectionResetError:
                break
    finally:
        bus.unsubscribe(q)

    return ws


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

async def start_dashboard(event_bus: EventBus) -> web.AppRunner:
    """Start the dashboard web server. Returns the runner for cleanup."""
    app = web.Application()
    app["event_bus"] = event_bus
    app.router.add_get("/", _handle_index)
    app.router.add_get("/ws", _handle_ws)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host=Config.DASHBOARD_HOST,
        port=Config.DASHBOARD_PORT,
    )
    await site.start()

    log.info(
        "Dashboard running on http://%s:%d",
        Config.DASHBOARD_HOST,
        Config.DASHBOARD_PORT,
    )
    return runner
