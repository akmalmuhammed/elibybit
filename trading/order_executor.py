"""
Order Executor — Handles limit order placement with 3-tier escalation.

Tier 1 (0-15s):  PostOnly at best bid/ask → guaranteed maker fee (0.02%)
Tier 2 (15-30s): PostOnly at refreshed best bid/ask
Tier 3 (30-45s): Regular Limit at best bid/ask → may pay taker (0.055%)
"""

from __future__ import annotations
import asyncio
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime
from typing import Optional, Tuple, TYPE_CHECKING
from exchange.models import Trade, TradeStatus, Side, CoinInfo
import logging

if TYPE_CHECKING:
    from exchange.bybit_rest import BybitRestClient
    from config import ExecutionConfig

logger = logging.getLogger(__name__)


class OrderExecutor:
    """Handles order placement with intelligent fill management."""

    def __init__(self, client: "BybitRestClient", config: "ExecutionConfig"):
        self.client = client
        self.config = config

    async def execute_entry(
        self,
        trade: Trade,
        coin: CoinInfo,
        position_size_usdt: Decimal,
    ) -> bool:
        """
        Execute a trade entry with 3-tier limit order escalation.
        Returns True if filled, False if all attempts failed.
        """
        trade.status = TradeStatus.FILLING
        symbol = trade.symbol
        side = trade.side.value  # "Buy" or "Sell"

        for attempt in range(1, self.config.max_fill_retries + 1):
            trade.fill_attempts = attempt

            # Determine order type based on tier
            if attempt <= self.config.post_only_retries:
                time_in_force = "PostOnly"
                tier = attempt
            else:
                time_in_force = "GTC"  # Regular limit — may cross as taker
                tier = 3

            logger.info(
                f"[EXEC] {symbol}: Tier {tier} attempt (attempt {attempt}/{self.config.max_fill_retries})"
            )

            # Get current best bid/ask
            price = await self._get_entry_price(symbol, side)
            if price is None:
                logger.error(f"[EXEC] {symbol}: Failed to get orderbook")
                continue

            # Calculate quantity
            qty = self._calculate_qty(position_size_usdt, price, coin)
            if qty is None or qty <= 0:
                logger.error(f"[EXEC] {symbol}: Invalid qty calculation")
                return False

            # Round price to tick size
            price = self._round_price(price, coin.tick_size, side)

            logger.info(
                f"[EXEC] {symbol}: Placing {side} limit @ {price}, "
                f"qty={qty}, TIF={time_in_force}"
            )

            # Place order
            result = await self.client.place_order(
                symbol=symbol,
                side=side,
                qty=str(qty),
                price=str(price),
                order_type="Limit",
                time_in_force=time_in_force,
            )

            ret_code = result.get("retCode", -1)

            # PostOnly rejection (would be taker)
            if ret_code == 170213 or ret_code == 170217:
                logger.warning(f"[EXEC] {symbol}: PostOnly rejected (would cross book). Retrying...")
                await asyncio.sleep(1)
                continue

            # Other error
            if ret_code != 0:
                logger.error(f"[EXEC] {symbol}: Order error: {result.get('retMsg')}")
                await asyncio.sleep(1)
                continue

            # Order accepted
            order_id = result.get("result", {}).get("orderId")
            if not order_id:
                logger.error(f"[EXEC] {symbol}: No orderId in response")
                continue

            trade.order_id = order_id
            logger.info(f"[EXEC] {symbol}: Order placed, id={order_id}. Waiting for fill...")

            # Wait for fill
            filled = await self._wait_for_fill(symbol, order_id)

            if filled:
                trade.entry_price = price
                trade.qty = qty
                trade.entry_time = datetime.utcnow()
                trade.status = TradeStatus.OPEN
                logger.info(
                    f"[EXEC] {symbol}: FILLED ✓ {side} {qty} @ {price} "
                    f"(Tier {tier})"
                )
                return True
            else:
                # Cancel unfilled order
                logger.warning(f"[EXEC] {symbol}: Not filled in {self.config.fill_timeout_sec}s. Cancelling...")
                await self.client.cancel_order(symbol, order_id)
                await asyncio.sleep(0.5)

        # All attempts exhausted
        logger.warning(f"[EXEC] {symbol}: All {self.config.max_fill_retries} attempts failed. Giving up.")
        trade.status = TradeStatus.CANCELLED
        return False

    async def close_position_market(self, trade: Trade) -> bool:
        """Force close a position at market (for kill switch)."""
        if not trade.qty:
            return False

        result = await self.client.close_position_market(
            symbol=trade.symbol,
            side=trade.side.value,
            qty=str(trade.qty),
        )
        return result.get("retCode") == 0

    async def _get_entry_price(self, symbol: str, side: str) -> Optional[Decimal]:
        """Get the appropriate price for entry."""
        ob = await self.client.get_orderbook(symbol, limit=1)
        bids = ob.get("b", [])
        asks = ob.get("a", [])

        if not bids or not asks:
            return None

        if side == "Buy":
            # Long: place at best bid
            return Decimal(bids[0][0])
        else:
            # Short: place at best ask
            return Decimal(asks[0][0])

    async def _wait_for_fill(self, symbol: str, order_id: str) -> bool:
        """
        Poll for order fill status.
        Returns True if fully filled within timeout.
        """
        deadline = asyncio.get_event_loop().time() + self.config.fill_timeout_sec
        check_interval = 1.0  # Check every second

        while asyncio.get_event_loop().time() < deadline:
            orders = await self.client.get_open_orders(symbol)

            # If order is no longer in open orders, it was filled (or cancelled)
            order_found = False
            for order in orders:
                if order.get("orderId") == order_id:
                    order_found = True
                    status = order.get("orderStatus", "")
                    if status == "Filled":
                        return True
                    elif status in ("Cancelled", "Rejected", "Deactivated"):
                        return False
                    break

            # Order not in open orders — likely filled
            if not order_found:
                return True

            await asyncio.sleep(check_interval)

        return False

    def _calculate_qty(
        self,
        position_size_usdt: Decimal,
        price: Decimal,
        coin: CoinInfo,
    ) -> Optional[Decimal]:
        """
        Calculate order quantity from USDT position size.
        qty = position_size / price, rounded to qty_step.
        """
        if price <= 0:
            return None

        raw_qty = position_size_usdt / price

        # Round down to qty_step
        if coin.qty_step > 0:
            qty = (raw_qty / coin.qty_step).to_integral_value(rounding=ROUND_DOWN) * coin.qty_step
        else:
            qty = raw_qty

        # Check minimum
        if qty < coin.min_qty:
            logger.warning(
                f"[EXEC] {coin.symbol}: Calculated qty {qty} < min {coin.min_qty}"
            )
            return None

        return qty

    def _round_price(self, price: Decimal, tick_size: Decimal, side: str) -> Decimal:
        """
        Round price to tick size.
        Buy: round down (more favorable)
        Sell: round up (more favorable)
        """
        if tick_size <= 0:
            return price

        if side == "Buy":
            return (price / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size
        else:
            return (price / tick_size).to_integral_value(rounding=ROUND_UP) * tick_size
