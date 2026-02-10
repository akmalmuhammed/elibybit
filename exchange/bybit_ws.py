"""
Bybit WebSocket Manager.
Handles public (klines, tickers) and private (orders, positions) streams.
Auto-reconnects on disconnect.
"""

from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
import websockets
import logging

logger = logging.getLogger(__name__)

# Type for async callback: receives (topic, data)
WSCallback = Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]]


class BybitWSManager:
    """Manages Bybit V5 WebSocket connections."""

    def __init__(
        self,
        public_url: str,
        private_url: str,
        api_key: str = "",
        api_secret: str = "",
    ):
        self.public_url = public_url
        self.private_url = private_url
        self.api_key = api_key
        self.api_secret = api_secret

        self._public_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._private_ws: Optional[websockets.WebSocketClientProtocol] = None

        self._public_subs: Set[str] = set()
        self._private_subs: Set[str] = set()

        self._callbacks: Dict[str, List[WSCallback]] = {}
        self._running = False
        self._ping_interval = 20  # seconds

    def on(self, topic_prefix: str, callback: WSCallback):
        """
        Register a callback for a topic prefix.
        E.g., on("kline.240", handler) will match "kline.240.BTCUSDT"
        """
        if topic_prefix not in self._callbacks:
            self._callbacks[topic_prefix] = []
        self._callbacks[topic_prefix].append(callback)

    async def start(self):
        """Start both public and private WebSocket connections."""
        self._running = True
        await asyncio.gather(
            self._run_public(),
            self._run_private(),
        )

    async def stop(self):
        """Gracefully stop all connections."""
        self._running = False
        if self._public_ws:
            await self._public_ws.close()
        if self._private_ws:
            await self._private_ws.close()

    async def subscribe_public(self, topics: List[str]):
        """Subscribe to public topics."""
        new_topics = [t for t in topics if t not in self._public_subs]
        if not new_topics:
            return

        self._public_subs.update(new_topics)

        if self._public_ws:
            msg = {"op": "subscribe", "args": new_topics}
            await self._public_ws.send(json.dumps(msg))
            logger.info(f"[WS-PUB] Subscribed: {new_topics}")

    async def unsubscribe_public(self, topics: List[str]):
        """Unsubscribe from public topics."""
        existing = [t for t in topics if t in self._public_subs]
        if not existing:
            return

        self._public_subs -= set(existing)

        if self._public_ws:
            msg = {"op": "unsubscribe", "args": existing}
            await self._public_ws.send(json.dumps(msg))
            logger.info(f"[WS-PUB] Unsubscribed: {existing}")

    async def subscribe_symbols(self, symbols: List[str]):
        """Subscribe to all needed streams for a list of symbols."""
        topics = []
        for symbol in symbols:
            topics.extend([
                f"kline.240.{symbol}",   # 4H candles for HA
                f"kline.15.{symbol}",    # 15M candles for ATR
                f"tickers.{symbol}",     # Price updates for TP monitoring
            ])
        await self.subscribe_public(topics)

    async def unsubscribe_symbols(self, symbols: List[str]):
        """Unsubscribe all streams for a list of symbols."""
        topics = []
        for symbol in symbols:
            topics.extend([
                f"kline.240.{symbol}",
                f"kline.15.{symbol}",
                f"tickers.{symbol}",
            ])
        await self.unsubscribe_public(topics)

    # ==================== Internal Connection Management ====================

    async def _run_public(self):
        """Run public WebSocket with auto-reconnect."""
        while self._running:
            try:
                async with websockets.connect(
                    self.public_url,
                    ping_interval=self._ping_interval,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._public_ws = ws
                    logger.info(f"[WS-PUB] Connected to {self.public_url}")

                    # Resubscribe on reconnect
                    if self._public_subs:
                        msg = {"op": "subscribe", "args": list(self._public_subs)}
                        await ws.send(json.dumps(msg))
                        logger.info(f"[WS-PUB] Resubscribed to {len(self._public_subs)} topics")

                    async for raw in ws:
                        await self._handle_public_message(raw)

            except websockets.ConnectionClosed as e:
                logger.warning(f"[WS-PUB] Connection closed: {e}. Reconnecting in 3s...")
            except Exception as e:
                logger.error(f"[WS-PUB] Error: {e}. Reconnecting in 5s...")

            self._public_ws = None
            if self._running:
                await asyncio.sleep(3)

    async def _run_private(self):
        """Run private WebSocket with authentication and auto-reconnect."""
        while self._running:
            try:
                async with websockets.connect(
                    self.private_url,
                    ping_interval=self._ping_interval,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._private_ws = ws
                    logger.info(f"[WS-PRV] Connected to {self.private_url}")

                    # Authenticate
                    await self._authenticate(ws)

                    # Subscribe to private topics
                    private_topics = ["order", "execution", "position"]
                    msg = {"op": "subscribe", "args": private_topics}
                    await ws.send(json.dumps(msg))
                    logger.info(f"[WS-PRV] Subscribed to private topics")

                    async for raw in ws:
                        await self._handle_private_message(raw)

            except websockets.ConnectionClosed as e:
                logger.warning(f"[WS-PRV] Connection closed: {e}. Reconnecting in 3s...")
            except Exception as e:
                logger.error(f"[WS-PRV] Error: {e}. Reconnecting in 5s...")

            self._private_ws = None
            if self._running:
                await asyncio.sleep(3)

    async def _authenticate(self, ws):
        """Authenticate private WebSocket connection."""
        expires = int(time.time() * 1000) + 10000
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            f"GET/realtime{expires}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        auth_msg = {
            "op": "auth",
            "args": [self.api_key, expires, signature],
        }
        await ws.send(json.dumps(auth_msg))

        # Wait for auth response
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        data = json.loads(resp)
        if data.get("success"):
            logger.info("[WS-PRV] Authenticated successfully")
        else:
            logger.error(f"[WS-PRV] Authentication failed: {data}")
            raise ConnectionError("WebSocket authentication failed")

    async def _handle_public_message(self, raw: str):
        """Route public WebSocket messages to callbacks."""
        try:
            data = json.loads(raw)

            # Ignore pongs and subscription confirmations
            if "op" in data:
                if data.get("success") is False:
                    logger.error(f"[WS-PUB] Op failed: {data}")
                return

            topic = data.get("topic", "")
            if not topic:
                return

            await self._dispatch(topic, data)

        except json.JSONDecodeError:
            logger.warning(f"[WS-PUB] Invalid JSON: {raw[:100]}")
        except Exception as e:
            logger.error(f"[WS-PUB] Handler error: {e}", exc_info=True)

    async def _handle_private_message(self, raw: str):
        """Route private WebSocket messages to callbacks."""
        try:
            data = json.loads(raw)

            if "op" in data:
                return

            topic = data.get("topic", "")
            if not topic:
                return

            await self._dispatch(topic, data)

        except json.JSONDecodeError:
            logger.warning(f"[WS-PRV] Invalid JSON: {raw[:100]}")
        except Exception as e:
            logger.error(f"[WS-PRV] Handler error: {e}", exc_info=True)

    async def _dispatch(self, topic: str, data: Dict):
        """Dispatch message to matching callbacks."""
        for prefix, callbacks in self._callbacks.items():
            if topic.startswith(prefix):
                for cb in callbacks:
                    try:
                        await cb(topic, data)
                    except Exception as e:
                        logger.error(
                            f"[WS] Callback error for {prefix}: {e}",
                            exc_info=True,
                        )
