"""
Bybit V5 REST API Client.
Handles authentication, rate limiting, and all needed endpoints.
"""

from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional
import aiohttp
import logging

logger = logging.getLogger(__name__)


class BybitRestClient:
    """Async Bybit V5 REST API wrapper."""

    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self._session: Optional[aiohttp.ClientSession] = None
        self._recv_window = "5000"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, timestamp: str, params_str: str) -> str:
        """Generate HMAC-SHA256 signature."""
        param_str = f"{timestamp}{self.api_key}{self._recv_window}{params_str}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self, timestamp: str, signature: str) -> Dict[str, str]:
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self._recv_window,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        signed: bool = False,
    ) -> Dict[str, Any]:
        """Make an API request with optional authentication."""
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        timestamp = str(int(time.time() * 1000))

        headers = {"Content-Type": "application/json"}

        if signed:
            if method == "GET":
                query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
                sig = self._sign(timestamp, query)
                headers = self._auth_headers(timestamp, sig)
                url = f"{url}?{query}" if query else url
                params = None
            else:
                body_str = json.dumps(params or {})
                sig = self._sign(timestamp, body_str)
                headers = self._auth_headers(timestamp, sig)

        try:
            if method == "GET":
                async with session.get(url, headers=headers, params=params if not signed else None) as resp:
                    data = await resp.json()
            else:
                async with session.post(url, headers=headers, json=params) as resp:
                    data = await resp.json()

            if data.get("retCode") != 0:
                logger.error(
                    f"[REST] {method} {endpoint} Error: "
                    f"code={data.get('retCode')}, msg={data.get('retMsg')}"
                )
            return data

        except Exception as e:
            logger.error(f"[REST] {method} {endpoint} Exception: {e}")
            raise

    # ==================== Market Endpoints ====================

    async def get_tickers(self) -> List[Dict]:
        """Get all USDT perpetual tickers."""
        data = await self._request("GET", "/v5/market/tickers", {"category": "linear"})
        return data.get("result", {}).get("list", [])

    async def get_instruments_info(self) -> List[Dict]:
        """Get instrument specifications (min qty, tick size, etc.)."""
        data = await self._request(
            "GET", "/v5/market/instruments-info",
            {"category": "linear", "limit": "1000"},
        )
        return data.get("result", {}).get("list", [])

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 50,
    ) -> List[Dict]:
        """
        Get historical kline/candle data.
        Interval: 1, 3, 5, 15, 30, 60, 120, 240, 360, 720, D, W, M
        Returns newest first — caller should reverse for chronological order.
        """
        data = await self._request(
            "GET", "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": str(limit),
            },
        )
        return data.get("result", {}).get("list", [])

    async def get_orderbook(self, symbol: str, limit: int = 1) -> Dict:
        """Get orderbook (top of book)."""
        data = await self._request(
            "GET", "/v5/market/orderbook",
            {"category": "linear", "symbol": symbol, "limit": str(limit)},
        )
        return data.get("result", {})

    # ==================== Trading Endpoints ====================

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        price: Optional[str] = None,
        order_type: str = "Limit",
        time_in_force: str = "PostOnly",
        reduce_only: bool = False,
        stop_loss: Optional[str] = None,
        position_idx: int = 0,
    ) -> Dict:
        """Place an order."""
        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "positionIdx": position_idx,
        }
        if price:
            params["price"] = price
        if time_in_force and order_type == "Limit":
            params["timeInForce"] = time_in_force
        if reduce_only:
            params["reduceOnly"] = True
        if stop_loss:
            params["stopLoss"] = stop_loss

        logger.info(f"[ORDER] Placing: {side} {qty} {symbol} @ {price or 'Market'} ({order_type})")
        data = await self._request("POST", "/v5/order/create", params, signed=True)
        return data

    async def cancel_order(self, symbol: str, order_id: str) -> Dict:
        """Cancel an order."""
        params = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id,
        }
        logger.info(f"[ORDER] Cancelling: {order_id} on {symbol}")
        return await self._request("POST", "/v5/order/cancel", params, signed=True)

    async def amend_order(
        self,
        symbol: str,
        order_id: str,
        price: Optional[str] = None,
        qty: Optional[str] = None,
        trigger_price: Optional[str] = None,
    ) -> Dict:
        """Amend an existing order (price, qty, or trigger)."""
        params = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id,
        }
        if price:
            params["price"] = price
        if qty:
            params["qty"] = qty
        if trigger_price:
            params["triggerPrice"] = trigger_price

        logger.info(f"[ORDER] Amending: {order_id} on {symbol}")
        return await self._request("POST", "/v5/order/amend", params, signed=True)

    async def set_trading_stop(
        self,
        symbol: str,
        stop_loss: Optional[str] = None,
        take_profit: Optional[str] = None,
        position_idx: int = 0,
    ) -> Dict:
        """Set/update SL/TP on an existing position."""
        params = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": position_idx,
        }
        if stop_loss:
            params["stopLoss"] = stop_loss
        if take_profit:
            params["takeProfit"] = take_profit

        logger.info(f"[ORDER] Setting trading stop: {symbol} SL={stop_loss}")
        return await self._request("POST", "/v5/position/set-trading-stop", params, signed=True)

    async def get_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get open positions."""
        params = {"category": "linear", "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/v5/position/list", params, signed=True)
        return data.get("result", {}).get("list", [])

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get open/active orders."""
        params = {"category": "linear"}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/v5/order/realtime", params, signed=True)
        return data.get("result", {}).get("list", [])

    async def get_wallet_balance(self) -> Dict:
        """Get unified account wallet balance."""
        data = await self._request(
            "GET", "/v5/account/wallet-balance",
            {"accountType": "UNIFIED"},
            signed=True,
        )
        return data.get("result", {})

    async def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """Set leverage for a symbol."""
        params = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        logger.info(f"[LEVERAGE] Setting {symbol} to {leverage}x")
        return await self._request("POST", "/v5/position/set-leverage", params, signed=True)

    async def close_position_market(self, symbol: str, side: str, qty: str) -> Dict:
        """Force close a position with a market order."""
        close_side = "Sell" if side == "Buy" else "Buy"
        return await self.place_order(
            symbol=symbol,
            side=close_side,
            qty=qty,
            order_type="Market",
            time_in_force="GTC",
            reduce_only=True,
        )
