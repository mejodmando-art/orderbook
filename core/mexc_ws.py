"""
MEXC WebSocket manager.

Responsibilities:
- Maintain a persistent connection to wss://wbs.mexc.com/ws
- Send PING every 20 s to keep the connection alive (MEXC drops idle sockets)
- Auto-reconnect with exponential back-off on any disconnect
- Dispatch incoming messages to registered async callbacks

Usage:
    ws = MexcWebSocket()
    ws.subscribe("spot@public.bookTicker.v3.api@BTCUSDT", my_callback)
    asyncio.create_task(ws.run())
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from config.settings import MEXC_WS_URL, WS_HEARTBEAT_INTERVAL, WS_RECONNECT_DELAY

logger = logging.getLogger(__name__)

# Type alias for async message handlers
MessageHandler = Callable[[dict], Coroutine[Any, Any, None]]


class MexcWebSocket:
    """
    Single-connection WebSocket client for MEXC public streams.

    Supports multiple topic subscriptions; each topic maps to one handler.
    The connection is shared across all subscriptions to avoid opening
    multiple sockets for the same symbol.
    """

    def __init__(self) -> None:
        self._subscriptions: dict[str, MessageHandler] = {}
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running: bool = False
        self._reconnect_delay: float = WS_RECONNECT_DELAY

    # ── Public API ─────────────────────────────────────────────────────────────

    def subscribe(self, topic: str, handler: MessageHandler) -> None:
        """Register a handler for a MEXC stream topic."""
        self._subscriptions[topic] = handler
        logger.info("Subscribed to topic: %s", topic)

    def unsubscribe(self, topic: str) -> None:
        """Remove a topic subscription."""
        self._subscriptions.pop(topic, None)
        logger.info("Unsubscribed from topic: %s", topic)

    def stop(self) -> None:
        """Signal the run loop to exit cleanly."""
        self._running = False

    async def run(self) -> None:
        """
        Main loop: connect → subscribe → read messages → reconnect on failure.
        Runs until stop() is called.
        """
        self._running = True
        while self._running:
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                logger.info("WebSocket task cancelled")
                break
            except Exception as exc:
                logger.error("WebSocket error: %s — reconnecting in %ss",
                             exc, self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                # Exponential back-off capped at 60 s
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)
            else:
                # Clean disconnect: reset delay
                self._reconnect_delay = WS_RECONNECT_DELAY

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _connect_and_serve(self) -> None:
        """Open one WebSocket session and serve until it closes."""
        logger.info("Connecting to %s", MEXC_WS_URL)
        async with websockets.connect(
            MEXC_WS_URL,
            ping_interval=None,   # we manage heartbeats manually
            ping_timeout=None,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._reconnect_delay = WS_RECONNECT_DELAY  # reset on success
            logger.info("WebSocket connected")

            await self._send_subscriptions(ws)

            # Run heartbeat and message reader concurrently
            await asyncio.gather(
                self._heartbeat_loop(ws),
                self._read_loop(ws),
            )

    async def _send_subscriptions(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send SUBSCRIBE frames for all registered topics."""
        if not self._subscriptions:
            return
        payload = {
            "method": "SUBSCRIPTION",
            "params": list(self._subscriptions.keys()),
        }
        await ws.send(json.dumps(payload))
        logger.debug("Sent subscriptions: %s", list(self._subscriptions.keys()))

    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send a PING frame every WS_HEARTBEAT_INTERVAL seconds."""
        while self._running:
            await asyncio.sleep(WS_HEARTBEAT_INTERVAL)
            try:
                ping_msg = json.dumps({"method": "PING"})
                await ws.send(ping_msg)
                logger.debug("PING sent at %s", time.strftime("%H:%M:%S"))
            except ConnectionClosed:
                break

    async def _read_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Read messages and dispatch to the matching handler."""
        async for raw in ws:
            if not self._running:
                break
            try:
                msg: dict = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Non-JSON message received: %s", raw)
                continue

            # MEXC sends {"msg": "PONG"} in response to our PING
            if msg.get("msg") == "PONG":
                logger.debug("PONG received")
                continue

            channel: str = msg.get("c", "")
            handler = self._subscriptions.get(channel)
            if handler:
                try:
                    await handler(msg)
                except Exception as exc:
                    logger.exception("Handler error for channel %s: %s", channel, exc)
            else:
                logger.debug("No handler for channel: %s", channel)

    async def send_raw(self, payload: dict) -> None:
        """Send an arbitrary JSON payload (e.g. for dynamic re-subscription)."""
        if self._ws and not self._ws.closed:
            await self._ws.send(json.dumps(payload))
        else:
            logger.warning("send_raw called but WebSocket is not connected")
